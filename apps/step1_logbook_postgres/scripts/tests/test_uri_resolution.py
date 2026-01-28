# -*- coding: utf-8 -*-
"""
test_uri_resolution.py - URI 解析与规范化模块单元测试

测试覆盖:
1. parse_uri: 各种 scheme (file, http, s3, gs, artifact 等) 解析
2. normalize_uri: 路径规范化（分隔符、相对路径等）
3. classify_uri: URI 类型分类
4. is_remote_uri / is_local_uri: 远程/本地判断
5. resolve_to_local_path: 使用 tmp_path 临时目录测试 URI 解析到本地路径
6. build_artifact_uri: artifact URI 构建

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

from engram_step1.uri import (
    ParsedUri,
    UriType,
    build_artifact_uri,
    classify_uri,
    get_uri_path,
    is_local_uri,
    is_remote_uri,
    normalize_uri,
    parse_uri,
    resolve_to_local_path,
    parse_scm_artifact_path,
    resolve_scm_artifact_path,
    ScmArtifactPath,
)


class TestParseUri:
    """测试 parse_uri 函数"""

    def test_parse_file_uri_unix(self):
        """测试 Unix 风格 file:// URI"""
        result = parse_uri("file:///home/user/file.txt")
        assert result.scheme == "file"
        assert result.uri_type == UriType.FILE
        assert result.path == "/home/user/file.txt"
        assert result.is_local is True
        assert result.is_remote is False

    def test_parse_file_uri_windows(self):
        """测试 Windows 风格 file:// URI"""
        result = parse_uri("file:///C:/Users/test/file.txt")
        assert result.scheme == "file"
        assert result.uri_type == UriType.FILE
        # Windows 路径处理：移除前导斜杠
        assert result.path == "C:/Users/test/file.txt"
        assert result.is_local is True

    def test_parse_http_uri(self):
        """测试 HTTP URI"""
        result = parse_uri("http://example.com/path/file.txt")
        assert result.scheme == "http"
        assert result.uri_type == UriType.HTTP
        assert result.is_remote is True
        assert result.is_local is False

    def test_parse_https_uri(self):
        """测试 HTTPS URI"""
        result = parse_uri("https://secure.example.com/api/data")
        assert result.scheme == "https"
        assert result.uri_type == UriType.HTTP
        assert result.is_remote is True

    def test_parse_s3_uri(self):
        """测试 S3 URI"""
        result = parse_uri("s3://my-bucket/path/to/object.txt")
        assert result.scheme == "s3"
        assert result.uri_type == UriType.S3
        # S3 URI 解析格式：netloc/path，可能产生双斜杠
        assert "my-bucket" in result.path
        assert "object.txt" in result.path
        assert result.is_remote is True

    def test_parse_gs_uri(self):
        """测试 Google Cloud Storage URI"""
        result = parse_uri("gs://gcs-bucket/folder/file.json")
        assert result.scheme == "gs"
        assert result.uri_type == UriType.GS
        # GS URI 解析格式：netloc/path，可能产生双斜杠
        assert "gcs-bucket" in result.path
        assert "file.json" in result.path
        assert result.is_remote is True

    def test_parse_ftp_uri(self):
        """测试 FTP URI"""
        result = parse_uri("ftp://ftp.example.com/pub/file.zip")
        assert result.scheme == "ftp"
        assert result.uri_type == UriType.FTP
        assert result.is_remote is True

    def test_parse_artifact_uri_no_scheme(self):
        """测试无 scheme 的 artifact URI"""
        result = parse_uri("scm/repo-001/git/patch.diff")
        assert result.scheme is None
        assert result.uri_type == UriType.ARTIFACT
        assert result.path == "scm/repo-001/git/patch.diff"
        assert result.is_local is True
        assert result.is_remote is False

    def test_parse_artifact_uri_relative_path(self):
        """测试相对路径形式的 artifact URI"""
        result = parse_uri("patches/2024/01/fix.patch")
        assert result.uri_type == UriType.ARTIFACT
        assert result.path == "patches/2024/01/fix.patch"
        assert result.is_local is True

    def test_parse_artifact_uri_with_scheme(self):
        """测试 artifact:// scheme 的 URI"""
        result = parse_uri("artifact://scm/repo/test.diff")
        assert result.scheme == "artifact"
        assert result.uri_type == UriType.ARTIFACT
        assert result.path == "scm/repo/test.diff"
        assert result.is_local is True
        assert result.is_remote is False

    def test_parse_artifact_uri_with_scheme_simple(self):
        """测试简单的 artifact:// URI"""
        result = parse_uri("artifact://test.diff")
        assert result.scheme == "artifact"
        assert result.uri_type == UriType.ARTIFACT
        assert result.path == "test.diff"
        assert result.is_local is True

    def test_parse_artifact_uri_with_scheme_nested(self):
        """测试嵌套路径的 artifact:// URI"""
        result = parse_uri("artifact://scm/project/repo/git/commits/abc123.diff")
        assert result.scheme == "artifact"
        assert result.uri_type == UriType.ARTIFACT
        assert result.path == "scm/project/repo/git/commits/abc123.diff"
        assert result.is_local is True

    def test_parse_unknown_scheme(self):
        """测试未知 scheme"""
        result = parse_uri("custom://some/resource")
        assert result.scheme == "custom"
        assert result.uri_type == UriType.UNKNOWN
        # 未知 scheme 保守处理为远程
        assert result.is_remote is True

    def test_parse_uri_whitespace_handling(self):
        """测试 URI 空白字符处理"""
        result = parse_uri("  file:///path/to/file.txt  ")
        assert result.scheme == "file"
        assert result.path == "/path/to/file.txt"

    def test_parse_uri_raw_preserved(self):
        """测试原始 URI 保留"""
        uri = "https://example.com/path?query=value"
        result = parse_uri(uri)
        assert result.raw == uri


class TestNormalizeUri:
    """测试 normalize_uri 函数"""

    def test_normalize_backslash_to_slash(self):
        """测试反斜杠转换为正斜杠"""
        result = normalize_uri("scm\\repo-001\\git\\file.txt")
        assert result == "scm/repo-001/git/file.txt"

    def test_normalize_removes_leading_slash(self):
        """测试移除前导斜杠"""
        result = normalize_uri("/absolute/path/file.txt")
        assert result == "absolute/path/file.txt"

    def test_normalize_removes_trailing_slash(self):
        """测试移除尾部斜杠"""
        result = normalize_uri("path/to/dir/")
        assert result == "path/to/dir"

    def test_normalize_resolves_dot_components(self):
        """测试解析 . 路径组件"""
        result = normalize_uri("./scm/./repo/file.txt")
        assert result == "scm/repo/file.txt"

    def test_normalize_resolves_dotdot_components(self):
        """测试解析 .. 路径组件"""
        result = normalize_uri("scm/repo-001/../repo-002/file.txt")
        assert result == "scm/repo-002/file.txt"

    def test_normalize_path_object(self):
        """测试 Path 对象输入"""
        result = normalize_uri(Path("scm/repo/file.txt"))
        assert result == "scm/repo/file.txt"

    def test_normalize_empty_path(self):
        """测试空路径"""
        assert normalize_uri("") == ""
        assert normalize_uri(".") == ""
        assert normalize_uri("./") == ""

    def test_normalize_multiple_slashes(self):
        """测试多重斜杠"""
        result = normalize_uri("scm//repo///file.txt")
        assert result == "scm/repo/file.txt"


class TestClassifyUri:
    """测试 classify_uri 函数"""

    def test_classify_file_uri(self):
        """测试 file:// URI 分类"""
        assert classify_uri("file:///path/file.txt") == UriType.FILE

    def test_classify_http_uri(self):
        """测试 HTTP URI 分类"""
        assert classify_uri("http://example.com") == UriType.HTTP
        assert classify_uri("https://example.com") == UriType.HTTP

    def test_classify_s3_uri(self):
        """测试 S3 URI 分类"""
        assert classify_uri("s3://bucket/key") == UriType.S3

    def test_classify_gs_uri(self):
        """测试 GS URI 分类"""
        assert classify_uri("gs://bucket/key") == UriType.GS

    def test_classify_artifact_uri(self):
        """测试 artifact URI 分类"""
        assert classify_uri("scm/repo/patch.diff") == UriType.ARTIFACT
        assert classify_uri("patches/file.txt") == UriType.ARTIFACT

    def test_classify_artifact_uri_with_scheme(self):
        """测试 artifact:// scheme URI 分类"""
        assert classify_uri("artifact://scm/repo/patch.diff") == UriType.ARTIFACT
        assert classify_uri("artifact://test.diff") == UriType.ARTIFACT


class TestIsRemoteLocalUri:
    """测试 is_remote_uri 和 is_local_uri 函数"""

    def test_is_remote_uri(self):
        """测试远程 URI 判断"""
        assert is_remote_uri("http://example.com") is True
        assert is_remote_uri("https://example.com") is True
        assert is_remote_uri("s3://bucket/key") is True
        assert is_remote_uri("gs://bucket/key") is True
        assert is_remote_uri("ftp://ftp.example.com") is True
        assert is_remote_uri("custom://resource") is True

    def test_is_local_uri(self):
        """测试本地 URI 判断"""
        assert is_local_uri("file:///path/file.txt") is True
        assert is_local_uri("scm/repo/patch.diff") is True
        assert is_local_uri("patches/file.txt") is True

    def test_is_local_uri_artifact_scheme(self):
        """测试 artifact:// scheme 是本地 URI"""
        assert is_local_uri("artifact://scm/repo/test.diff") is True
        assert is_local_uri("artifact://test.diff") is True
        assert is_remote_uri("artifact://scm/repo/test.diff") is False

    def test_remote_and_local_mutually_exclusive(self):
        """测试远程和本地判断互斥"""
        for uri in ["http://example.com", "s3://bucket/key"]:
            assert is_remote_uri(uri) is True
            assert is_local_uri(uri) is False

        for uri in ["file:///path/file", "scm/repo/patch"]:
            assert is_local_uri(uri) is True
            assert is_remote_uri(uri) is False


class TestResolveToLocalPath:
    """测试 resolve_to_local_path 函数"""

    def test_resolve_file_uri_existing_file(self, tmp_path: Path):
        """测试解析 file:// URI 到存在的文件"""
        # 创建临时文件
        test_file = tmp_path / "test_file.txt"
        test_file.write_text("test content")

        # 构建 file:// URI
        if os.name == "nt":
            file_uri = f"file:///{test_file}".replace("\\", "/")
        else:
            file_uri = f"file://{test_file}"

        result = resolve_to_local_path(file_uri)
        assert result is not None
        assert Path(result).exists()
        assert Path(result).read_text() == "test content"

    def test_resolve_file_uri_nonexistent_file(self):
        """测试解析 file:// URI 到不存在的文件"""
        result = resolve_to_local_path("file:///nonexistent/path/file.txt")
        assert result is None

    def test_resolve_artifact_uri_with_artifacts_root(self, tmp_path: Path):
        """测试使用 artifacts_root 解析 artifact URI"""
        # 创建 artifacts 目录结构
        artifacts_root = tmp_path / "artifacts"
        scm_dir = artifacts_root / "scm" / "repo-001" / "git"
        scm_dir.mkdir(parents=True)

        # 创建测试文件
        patch_file = scm_dir / "patch.diff"
        patch_file.write_text("diff --git a/file.txt b/file.txt")

        # 解析 artifact URI
        result = resolve_to_local_path(
            "scm/repo-001/git/patch.diff",
            artifacts_root=artifacts_root,
        )

        assert result is not None
        assert Path(result).exists()
        assert "patch.diff" in result

    def test_resolve_artifact_uri_nonexistent(self, tmp_path: Path):
        """测试解析不存在的 artifact URI"""
        artifacts_root = tmp_path / "artifacts"
        artifacts_root.mkdir()

        result = resolve_to_local_path(
            "scm/nonexistent/patch.diff",
            artifacts_root=artifacts_root,
        )
        assert result is None

    def test_resolve_remote_uri_returns_none(self):
        """测试远程 URI 返回 None"""
        assert resolve_to_local_path("http://example.com/file.txt") is None
        assert resolve_to_local_path("https://example.com/file.txt") is None
        assert resolve_to_local_path("s3://bucket/key") is None
        assert resolve_to_local_path("gs://bucket/key") is None

    def test_resolve_artifact_uri_normalized_path(self, tmp_path: Path):
        """测试 artifact URI 路径规范化后解析"""
        artifacts_root = tmp_path / "artifacts"
        target_dir = artifacts_root / "scm" / "repo"
        target_dir.mkdir(parents=True)

        test_file = target_dir / "file.txt"
        test_file.write_text("content")

        # 使用非规范化路径
        result = resolve_to_local_path(
            "scm//repo/./file.txt",
            artifacts_root=artifacts_root,
        )

        assert result is not None
        assert Path(result).exists()

    def test_resolve_artifact_uri_with_scheme(self, tmp_path: Path):
        """测试解析 artifact:// scheme 的 URI"""
        # 创建 artifacts 目录结构
        artifacts_root = tmp_path / "artifacts"
        scm_dir = artifacts_root / "scm" / "repo"
        scm_dir.mkdir(parents=True)

        # 创建测试文件
        test_file = scm_dir / "test.diff"
        test_file.write_text("diff content")

        # 解析 artifact:// URI
        result = resolve_to_local_path(
            "artifact://scm/repo/test.diff",
            artifacts_root=artifacts_root,
        )

        assert result is not None
        assert Path(result).exists()
        assert Path(result).read_text() == "diff content"

    def test_resolve_artifact_uri_with_scheme_nonexistent(self, tmp_path: Path):
        """测试解析不存在的 artifact:// URI"""
        artifacts_root = tmp_path / "artifacts"
        artifacts_root.mkdir()

        result = resolve_to_local_path(
            "artifact://scm/nonexistent/test.diff",
            artifacts_root=artifacts_root,
        )
        assert result is None


class TestGetUriPath:
    """测试 get_uri_path 函数"""

    def test_get_path_from_file_uri(self):
        """测试从 file:// URI 获取路径"""
        path = get_uri_path("file:///home/user/file.txt")
        assert path == "/home/user/file.txt"

    def test_get_path_from_s3_uri(self):
        """测试从 S3 URI 获取路径"""
        path = get_uri_path("s3://bucket/path/to/object")
        # S3 URI 解析格式：netloc/path
        assert "bucket" in path
        assert "object" in path

    def test_get_path_from_artifact_uri(self):
        """测试从 artifact URI 获取路径"""
        path = get_uri_path("scm/repo-001/git/patch.diff")
        assert path == "scm/repo-001/git/patch.diff"


class TestBuildArtifactUri:
    """测试 build_artifact_uri 函数"""

    def test_build_simple_uri(self):
        """测试构建简单 artifact URI"""
        uri = build_artifact_uri("scm", "repo-001", "git", "patch.diff")
        assert uri == "scm/repo-001/git/patch.diff"

    def test_build_uri_normalizes_path(self):
        """测试构建 URI 时规范化路径"""
        uri = build_artifact_uri("scm", "./repo", "../patches", "file.diff")
        # 路径被规范化
        assert ".." not in uri or uri == "scm/patches/file.diff"

    def test_build_uri_single_part(self):
        """测试单个部分的 URI"""
        uri = build_artifact_uri("simple_file.txt")
        assert uri == "simple_file.txt"

    def test_build_uri_empty_parts(self):
        """测试空部分处理"""
        uri = build_artifact_uri("scm", "", "repo", "file.txt")
        # 空字符串被处理
        assert "scm" in uri and "repo" in uri and "file.txt" in uri


class TestParsedUriRepr:
    """测试 ParsedUri 的字符串表示"""

    def test_repr_format(self):
        """测试 __repr__ 格式"""
        parsed = parse_uri("scm/repo/file.txt")
        repr_str = repr(parsed)
        assert "ParsedUri" in repr_str
        assert "artifact" in repr_str
        assert "scm/repo/file.txt" in repr_str


class TestEdgeCases:
    """测试边界情况"""

    def test_empty_uri(self):
        """测试空 URI"""
        result = parse_uri("")
        assert result.uri_type == UriType.ARTIFACT
        assert result.path == ""

    def test_uri_with_query_string(self):
        """测试带查询字符串的 URI"""
        result = parse_uri("https://example.com/path?query=value&foo=bar")
        assert result.scheme == "https"
        assert result.uri_type == UriType.HTTP

    def test_uri_with_fragment(self):
        """测试带 fragment 的 URI"""
        result = parse_uri("https://example.com/path#section")
        assert result.scheme == "https"
        assert result.uri_type == UriType.HTTP

    def test_uri_case_insensitive_scheme(self):
        """测试 scheme 大小写不敏感"""
        result1 = parse_uri("HTTP://example.com")
        result2 = parse_uri("http://example.com")
        assert result1.uri_type == result2.uri_type == UriType.HTTP

    def test_deeply_nested_artifact_path(self):
        """测试深层嵌套的 artifact 路径"""
        uri = "scm/project/repo/src/main/java/com/example/Class.java"
        result = parse_uri(uri)
        assert result.uri_type == UriType.ARTIFACT
        assert result.path == uri


# ---------- S3 URI 审计测试 ----------

from unittest.mock import patch, MagicMock


class TestAuditS3UriResolution:
    """测试 ArtifactAuditor._get_store_for_uri 对 S3 URI 的处理"""

    def test_s3_uri_bucket_matches_config(self, monkeypatch):
        """测试 S3 URI bucket 与配置 bucket 一致时正常返回 key"""
        # 设置环境变量
        monkeypatch.setenv("ENGRAM_S3_BUCKET", "my-artifacts")
        monkeypatch.setenv("ENGRAM_S3_ENDPOINT", "https://s3.example.com")
        monkeypatch.setenv("ENGRAM_S3_ACCESS_KEY", "test-key")
        monkeypatch.setenv("ENGRAM_S3_SECRET_KEY", "test-secret")
        
        from artifact_audit import ArtifactAuditor
        from engram_step1.artifact_store import ObjectStore
        
        auditor = ArtifactAuditor()
        
        # Mock ObjectStore 避免实际 S3 连接
        with patch.object(auditor, '_object_store', None):
            store, resolved_uri = auditor._get_store_for_uri("s3://my-artifacts/path/to/object.diff")
        
        # 验证返回的是 ObjectStore 实例
        assert isinstance(store, ObjectStore)
        # 验证返回的是 key 而不是完整 URI
        assert resolved_uri == "path/to/object.diff"

    def test_s3_uri_bucket_mismatch_raises_error(self, monkeypatch):
        """测试 S3 URI bucket 与配置 bucket 不一致时抛出错误"""
        # 设置环境变量
        monkeypatch.setenv("ENGRAM_S3_BUCKET", "my-artifacts")
        
        from artifact_audit import ArtifactAuditor
        from engram_step1.artifact_store import ArtifactReadError
        
        auditor = ArtifactAuditor()
        
        # 尝试访问不同 bucket 的对象
        with pytest.raises(ArtifactReadError) as exc_info:
            auditor._get_store_for_uri("s3://other-bucket/path/to/object.diff")
        
        assert "拒绝跨 bucket 审计" in str(exc_info.value)
        assert "other-bucket" in str(exc_info.value)
        assert "my-artifacts" in str(exc_info.value)

    def test_s3_uri_no_bucket_configured_raises_error(self, monkeypatch):
        """测试未配置 bucket 时访问 S3 URI 抛出错误"""
        # 确保环境变量未设置
        monkeypatch.delenv("ENGRAM_S3_BUCKET", raising=False)
        
        # 清除可能的全局配置缓存
        import engram_step1.config as config_module
        config_module._global_config = None
        config_module._global_app_config = None
        
        from artifact_audit import ArtifactAuditor
        from engram_step1.artifact_store import ArtifactReadError
        
        auditor = ArtifactAuditor()
        
        with pytest.raises(ArtifactReadError) as exc_info:
            auditor._get_store_for_uri("s3://some-bucket/path/to/object.diff")
        
        assert "未配置 bucket" in str(exc_info.value)

    def test_s3_uri_missing_key_raises_error(self, monkeypatch):
        """测试 S3 URI 缺少 key 时抛出错误"""
        monkeypatch.setenv("ENGRAM_S3_BUCKET", "my-artifacts")
        
        from artifact_audit import ArtifactAuditor
        from engram_step1.artifact_store import ArtifactReadError
        
        auditor = ArtifactAuditor()
        
        # 只有 bucket 没有 key 的 URI（实际上 parse_uri 会处理为 bucket 没有 /）
        # 需要构造一个只有 bucket 的情况
        with pytest.raises(ArtifactReadError) as exc_info:
            auditor._get_store_for_uri("s3://bucket-only")
        
        assert "缺少对象 key" in str(exc_info.value)

    def test_s3_uri_with_nested_key(self, monkeypatch):
        """测试 S3 URI 带有嵌套路径的 key"""
        monkeypatch.setenv("ENGRAM_S3_BUCKET", "engram-artifacts")
        monkeypatch.setenv("ENGRAM_S3_ENDPOINT", "https://s3.example.com")
        monkeypatch.setenv("ENGRAM_S3_ACCESS_KEY", "test-key")
        monkeypatch.setenv("ENGRAM_S3_SECRET_KEY", "test-secret")
        
        from artifact_audit import ArtifactAuditor
        from engram_step1.artifact_store import ObjectStore
        
        auditor = ArtifactAuditor()
        
        with patch.object(auditor, '_object_store', None):
            store, resolved_uri = auditor._get_store_for_uri(
                "s3://engram-artifacts/scm/project/repo/git/abc123/sha256.diff"
            )
        
        assert isinstance(store, ObjectStore)
        assert resolved_uri == "scm/project/repo/git/abc123/sha256.diff"


# ---------- 运行测试的入口 ----------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
