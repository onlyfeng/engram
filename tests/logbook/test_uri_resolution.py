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

from engram.logbook.uri import (
    ATTACHMENT_URI_ERR_INVALID_ID,
    ATTACHMENT_URI_ERR_INVALID_SHA256,
    ATTACHMENT_URI_ERR_LEGACY_FORMAT,
    ATTACHMENT_URI_ERR_MALFORMED,
    ATTACHMENT_URI_ERR_NOT_ATTACHMENTS,
    ATTACHMENT_URI_ERR_NOT_MEMORY,
    UriType,
    build_artifact_uri,
    # Attachment URI 相关
    build_attachment_evidence_uri,
    build_evidence_uri,
    build_evidence_uri_from_patch_blob,
    classify_uri,
    get_uri_path,
    is_local_uri,
    is_patch_blob_evidence_uri,
    is_remote_uri,
    normalize_uri,
    parse_attachment_evidence_uri,
    parse_attachment_evidence_uri_strict,
    parse_evidence_uri,
    parse_uri,
    resolve_to_local_path,
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


# ---------- Patch Blobs Evidence URI 测试 ----------


# 测试用的有效 SHA256（64 位十六进制）
VALID_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class TestBuildEvidenceUri:
    """测试 build_evidence_uri 和 build_evidence_uri_from_patch_blob 函数"""

    def test_build_evidence_uri_git(self):
        """测试构建 Git patch_blobs evidence URI"""
        uri = build_evidence_uri("git", "1:abc123def", VALID_SHA256)
        assert uri == f"memory://patch_blobs/git/1:abc123def/{VALID_SHA256}"

    def test_build_evidence_uri_svn(self):
        """测试构建 SVN patch_blobs evidence URI"""
        uri = build_evidence_uri("svn", "2:1234", VALID_SHA256)
        assert uri == f"memory://patch_blobs/svn/2:1234/{VALID_SHA256}"

    def test_build_evidence_uri_normalizes_case(self):
        """测试 source_type 和 sha256 自动转为小写"""
        uri = build_evidence_uri("GIT", "1:ABC", VALID_SHA256.upper())
        assert uri == f"memory://patch_blobs/git/1:ABC/{VALID_SHA256}"

    def test_build_evidence_uri_from_patch_blob(self):
        """测试 build_evidence_uri_from_patch_blob 便捷方法"""
        uri = build_evidence_uri_from_patch_blob("git", 1, "abc123def", VALID_SHA256)
        assert uri == f"memory://patch_blobs/git/1:abc123def/{VALID_SHA256}"

    def test_build_evidence_uri_from_patch_blob_svn(self):
        """测试 SVN 的 build_evidence_uri_from_patch_blob"""
        uri = build_evidence_uri_from_patch_blob("svn", 2, "1234", VALID_SHA256)
        assert uri == f"memory://patch_blobs/svn/2:1234/{VALID_SHA256}"


class TestParseEvidenceUri:
    """测试 parse_evidence_uri 函数"""

    def test_parse_valid_git_uri(self):
        """测试解析有效的 Git patch_blobs evidence URI"""
        uri = f"memory://patch_blobs/git/1:abc123/{VALID_SHA256}"
        result = parse_evidence_uri(uri)

        assert result is not None
        assert result["source_type"] == "git"
        assert result["source_id"] == "1:abc123"
        assert result["sha256"] == VALID_SHA256

    def test_parse_valid_svn_uri(self):
        """测试解析有效的 SVN patch_blobs evidence URI"""
        uri = f"memory://patch_blobs/svn/2:1234/{VALID_SHA256}"
        result = parse_evidence_uri(uri)

        assert result is not None
        assert result["source_type"] == "svn"
        assert result["source_id"] == "2:1234"
        assert result["sha256"] == VALID_SHA256

    def test_parse_returns_none_for_non_memory(self):
        """测试非 memory:// scheme 返回 None"""
        assert parse_evidence_uri(f"https://patch_blobs/git/1:abc/{VALID_SHA256}") is None

    def test_parse_returns_none_for_non_patch_blobs(self):
        """测试非 patch_blobs 路径返回 None"""
        assert parse_evidence_uri(f"memory://attachments/123/{VALID_SHA256}") is None

    def test_parse_returns_none_for_malformed_path(self):
        """测试格式错误的路径返回 None"""
        # 缺少 sha256
        assert parse_evidence_uri("memory://patch_blobs/git/1:abc") is None
        # 只有 patch_blobs
        assert parse_evidence_uri("memory://patch_blobs") is None


class TestIsPatchBlobEvidenceUri:
    """测试 is_patch_blob_evidence_uri 函数"""

    def test_is_patch_blob_evidence_uri_true(self):
        """测试 patch_blobs URI 返回 True"""
        assert is_patch_blob_evidence_uri(f"memory://patch_blobs/git/1:abc/{VALID_SHA256}") is True
        assert is_patch_blob_evidence_uri(f"memory://patch_blobs/svn/2:1234/{VALID_SHA256}") is True

    def test_is_patch_blob_evidence_uri_false(self):
        """测试非 patch_blobs URI 返回 False"""
        assert is_patch_blob_evidence_uri(f"memory://attachments/123/{VALID_SHA256}") is False
        assert is_patch_blob_evidence_uri("https://example.com/patch_blobs") is False
        assert is_patch_blob_evidence_uri("scm/repo/patch.diff") is False


class TestPatchBlobsUriRoundTrip:
    """测试 patch_blobs build <-> parse 往返一致性"""

    def test_roundtrip_consistency(self):
        """测试 build 后 parse 应能正确还原"""
        source_type = "git"
        source_id = "1:abc123def"
        sha256 = VALID_SHA256

        # build
        uri = build_evidence_uri(source_type, source_id, sha256)

        # parse
        result = parse_evidence_uri(uri)

        assert result is not None
        assert result["source_type"] == source_type
        assert result["source_id"] == source_id
        assert result["sha256"] == sha256

    def test_roundtrip_with_different_source_types(self):
        """测试不同 source_type 的往返一致性"""
        for source_type in ["git", "svn", "gitlab"]:
            uri = build_evidence_uri(source_type, "1:abc", VALID_SHA256)
            result = parse_evidence_uri(uri)

            assert result is not None
            assert result["source_type"] == source_type


class TestPatchBlobsUriCompatibilityMatrix:
    """
    patch_blobs URI 参数化测试矩阵

    覆盖 evidence_packet.md 中的规范:
    - 格式: memory://patch_blobs/<source_type>/<source_id>/<sha256>
    """

    # 参数化测试: (uri, should_succeed, expected_source_type, expected_source_id, description)
    PATCH_BLOBS_CASES = [
        # ===== 正确格式: 应该成功 =====
        (
            f"memory://patch_blobs/git/1:abc123/{VALID_SHA256}",
            True,
            "git",
            "1:abc123",
            "Git 标准格式",
        ),
        (f"memory://patch_blobs/svn/2:1234/{VALID_SHA256}", True, "svn", "2:1234", "SVN 标准格式"),
        (
            f"memory://patch_blobs/gitlab/3:def456/{VALID_SHA256}",
            True,
            "gitlab",
            "3:def456",
            "GitLab 格式",
        ),
        # source_id 中包含特殊字符
        (
            f"memory://patch_blobs/git/1:abc123def456/{VALID_SHA256}",
            True,
            "git",
            "1:abc123def456",
            "长 SHA",
        ),
        (
            f"memory://patch_blobs/svn/99:99999/{VALID_SHA256}",
            True,
            "svn",
            "99:99999",
            "大 revision",
        ),
        # ===== 错误格式: 应该返回 None =====
        ("memory://patch_blobs/git/1:abc", None, None, None, "缺少 sha256"),
        ("memory://patch_blobs/git", None, None, None, "缺少 source_id 和 sha256"),
        ("memory://patch_blobs", None, None, None, "只有 patch_blobs 前缀"),
        (
            f"memory://attachments/123/{VALID_SHA256}",
            None,
            None,
            None,
            "attachments 而非 patch_blobs",
        ),
        (f"https://patch_blobs/git/1:abc/{VALID_SHA256}", None, None, None, "非 memory scheme"),
        (f"file:///patch_blobs/git/1:abc/{VALID_SHA256}", None, None, None, "file scheme"),
    ]

    @pytest.mark.parametrize(
        "uri,should_succeed,expected_source_type,expected_source_id,description", PATCH_BLOBS_CASES
    )
    def test_patch_blobs_uri_compatibility(
        self, uri, should_succeed, expected_source_type, expected_source_id, description
    ):
        """参数化测试: patch_blobs URI 格式兼容性"""
        result = parse_evidence_uri(uri)

        if should_succeed:
            assert result is not None, f"[{description}] 应该成功解析: {uri}"
            assert result["source_type"] == expected_source_type, (
                f"[{description}] source_type 不匹配"
            )
            assert result["source_id"] == expected_source_id, f"[{description}] source_id 不匹配"
            assert result["sha256"] == VALID_SHA256, f"[{description}] sha256 不匹配"
        else:
            assert result is None, f"[{description}] 应该返回 None: {uri}"


# ---------- Docs Evidence URI 测试 ----------
#
# 注意: docs URI 的解析函数尚未实现！
# 以下测试为 FAILING TESTS（锁定契约），待实现后应能通过。
#
# 规范格式: memory://docs/<rel_path>/<sha256>
# 示例: memory://docs/contracts/evidence_packet.md/a1b2c3d4e5f6...


class TestDocsEvidenceUri:
    """
    Docs Evidence URI 测试（契约锁定）

    根据 evidence_packet.md 规范:
    - 格式: memory://docs/<rel_path>/<sha256>
    - 用途: 规格/设计文档引用

    当前状态: 解析函数 parse_docs_evidence_uri() 尚未实现
    """

    @pytest.mark.xfail(reason="parse_docs_evidence_uri 尚未实现，待 implement task 完成")
    def test_parse_docs_evidence_uri_exists(self):
        """契约测试: parse_docs_evidence_uri 函数应存在"""
        from engram.logbook.uri import parse_docs_evidence_uri

        assert callable(parse_docs_evidence_uri)

    @pytest.mark.xfail(reason="build_docs_evidence_uri 尚未实现，待 implement task 完成")
    def test_build_docs_evidence_uri_exists(self):
        """契约测试: build_docs_evidence_uri 函数应存在"""
        from engram.logbook.uri import build_docs_evidence_uri

        assert callable(build_docs_evidence_uri)

    @pytest.mark.xfail(reason="is_docs_evidence_uri 尚未实现，待 implement task 完成")
    def test_is_docs_evidence_uri_exists(self):
        """契约测试: is_docs_evidence_uri 函数应存在"""
        from engram.logbook.uri import is_docs_evidence_uri

        assert callable(is_docs_evidence_uri)

    @pytest.mark.xfail(reason="parse_docs_evidence_uri 尚未实现，待 implement task 完成")
    def test_parse_docs_evidence_uri_contract(self):
        """契约测试: parse_docs_evidence_uri 应能解析标准格式

        规范: memory://docs/<rel_path>/<sha256>
        """
        from engram.logbook.uri import parse_docs_evidence_uri

        uri = f"memory://docs/contracts/evidence_packet.md/{VALID_SHA256}"
        result = parse_docs_evidence_uri(uri)

        assert result is not None
        assert result["rel_path"] == "contracts/evidence_packet.md"
        assert result["sha256"] == VALID_SHA256

    @pytest.mark.xfail(reason="build_docs_evidence_uri 尚未实现，待 implement task 完成")
    def test_build_docs_evidence_uri_contract(self):
        """契约测试: build_docs_evidence_uri 应能构建标准格式"""
        from engram.logbook.uri import build_docs_evidence_uri

        uri = build_docs_evidence_uri("contracts/evidence_packet.md", VALID_SHA256)
        assert uri == f"memory://docs/contracts/evidence_packet.md/{VALID_SHA256}"

    @pytest.mark.xfail(reason="docs URI 解析尚未实现，待 implement task 完成")
    def test_docs_uri_roundtrip_contract(self):
        """契约测试: docs URI build/parse 往返一致性"""
        from engram.logbook.uri import build_docs_evidence_uri, parse_docs_evidence_uri

        rel_path = "docs/architecture/overview.md"
        sha256 = VALID_SHA256

        # build
        uri = build_docs_evidence_uri(rel_path, sha256)

        # parse
        result = parse_docs_evidence_uri(uri)

        assert result is not None
        assert result["rel_path"] == rel_path
        assert result["sha256"] == sha256


class TestDocsEvidenceUriCompatibilityMatrix:
    """
    Docs URI 参数化测试矩阵（契约锁定）

    规范: memory://docs/<rel_path>/<sha256>

    当前状态: 解析函数尚未实现，所有测试标记为 xfail
    """

    # 参数化测试: (uri, should_succeed, expected_rel_path, description)
    DOCS_CASES = [
        # ===== 正确格式: 应该成功 =====
        (
            f"memory://docs/contracts/evidence_packet.md/{VALID_SHA256}",
            True,
            "contracts/evidence_packet.md",
            "规格文档",
        ),
        (
            f"memory://docs/docs/architecture.md/{VALID_SHA256}",
            True,
            "docs/architecture.md",
            "架构文档",
        ),
        (f"memory://docs/README.md/{VALID_SHA256}", True, "README.md", "根目录文档"),
        (f"memory://docs/a/b/c/deep.md/{VALID_SHA256}", True, "a/b/c/deep.md", "深层嵌套路径"),
        # ===== 错误格式: 应该返回 None =====
        ("memory://docs/file.md", None, None, "缺少 sha256"),
        ("memory://docs", None, None, "只有 docs 前缀"),
        (f"memory://patch_blobs/git/1:abc/{VALID_SHA256}", None, None, "patch_blobs 而非 docs"),
        (f"https://docs/file.md/{VALID_SHA256}", None, None, "非 memory scheme"),
    ]

    @pytest.mark.xfail(reason="parse_docs_evidence_uri 尚未实现，待 implement task 完成")
    @pytest.mark.parametrize("uri,should_succeed,expected_rel_path,description", DOCS_CASES)
    def test_docs_uri_compatibility(self, uri, should_succeed, expected_rel_path, description):
        """参数化测试: docs URI 格式兼容性"""
        from engram.logbook.uri import parse_docs_evidence_uri

        result = parse_docs_evidence_uri(uri)

        if should_succeed:
            assert result is not None, f"[{description}] 应该成功解析: {uri}"
            assert result["rel_path"] == expected_rel_path, f"[{description}] rel_path 不匹配"
            assert result["sha256"] == VALID_SHA256, f"[{description}] sha256 不匹配"
        else:
            assert result is None, f"[{description}] 应该返回 None: {uri}"


# ---------- Attachment Evidence URI 测试 ----------


class TestBuildAttachmentEvidenceUri:
    """测试 build_attachment_evidence_uri 函数"""

    def test_build_valid_uri(self):
        """测试构建有效的 attachment evidence URI"""
        uri = build_attachment_evidence_uri(12345, VALID_SHA256)
        assert uri == f"memory://attachments/12345/{VALID_SHA256}"

    def test_build_uri_normalizes_sha256(self):
        """测试 sha256 自动转为小写"""
        upper_sha = VALID_SHA256.upper()
        uri = build_attachment_evidence_uri(12345, upper_sha)
        assert uri == f"memory://attachments/12345/{VALID_SHA256}"

    def test_build_uri_rejects_non_int_id(self):
        """测试拒绝非整数 attachment_id"""
        with pytest.raises(ValueError) as exc_info:
            build_attachment_evidence_uri("abc", VALID_SHA256)
        assert "必须为整数" in str(exc_info.value)

    def test_build_uri_rejects_invalid_sha256_length(self):
        """测试拒绝非 64 位 sha256"""
        with pytest.raises(ValueError) as exc_info:
            build_attachment_evidence_uri(12345, "abc123")
        assert "64 位十六进制" in str(exc_info.value)

    def test_build_uri_rejects_invalid_sha256_chars(self):
        """测试拒绝非十六进制字符"""
        invalid_sha = "g" * 64  # 'g' 不是十六进制字符
        with pytest.raises(ValueError) as exc_info:
            build_attachment_evidence_uri(12345, invalid_sha)
        assert "64 位十六进制" in str(exc_info.value)


class TestParseAttachmentEvidenceUri:
    """测试 parse_attachment_evidence_uri 函数（简化接口）"""

    def test_parse_valid_uri(self):
        """测试解析有效的 attachment evidence URI"""
        uri = f"memory://attachments/12345/{VALID_SHA256}"
        result = parse_attachment_evidence_uri(uri)

        assert result is not None
        assert result["attachment_id"] == 12345
        assert result["sha256"] == VALID_SHA256

    def test_parse_returns_none_for_invalid_uri(self):
        """测试无效 URI 返回 None"""
        # 非 memory:// scheme
        assert parse_attachment_evidence_uri("https://example.com/attachments/123/sha") is None

        # 非 attachments 路径
        assert parse_attachment_evidence_uri(f"memory://patch_blobs/123/{VALID_SHA256}") is None

        # 旧格式（三段路径）
        assert parse_attachment_evidence_uri(f"memory://attachments/ns/123/{VALID_SHA256}") is None


class TestParseAttachmentEvidenceUriStrict:
    """测试 parse_attachment_evidence_uri_strict 函数（带详细错误信息）"""

    def test_parse_valid_uri(self):
        """测试解析有效的 attachment evidence URI"""
        uri = f"memory://attachments/12345/{VALID_SHA256}"
        result = parse_attachment_evidence_uri_strict(uri)

        assert result.success is True
        assert result.attachment_id == 12345
        assert result.sha256 == VALID_SHA256
        assert result.error_code is None
        assert result.error_message is None

    def test_parse_valid_uri_to_dict(self):
        """测试 to_dict 方法"""
        uri = f"memory://attachments/12345/{VALID_SHA256}"
        result = parse_attachment_evidence_uri_strict(uri)

        d = result.to_dict()
        assert d == {"attachment_id": 12345, "sha256": VALID_SHA256}

    def test_error_not_memory_scheme(self):
        """测试错误码: E_NOT_MEMORY（非 memory:// scheme）"""
        result = parse_attachment_evidence_uri_strict(
            f"https://example.com/attachments/123/{VALID_SHA256}"
        )

        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_NOT_MEMORY
        assert "memory://" in result.error_message

    def test_error_not_attachments_path(self):
        """测试错误码: E_NOT_ATTACHMENTS（路径不以 attachments/ 开头）"""
        result = parse_attachment_evidence_uri_strict(f"memory://patch_blobs/123/{VALID_SHA256}")

        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_NOT_ATTACHMENTS
        assert "attachments/" in result.error_message

    def test_error_legacy_format_three_segments(self):
        """测试错误码: E_LEGACY_FORMAT（旧格式三段路径）

        文档规范: ~~memory://attachments/<namespace>/<id>/<sha256>~~ 已废弃
        """
        # 旧格式: namespace/id/sha256 (三段路径)
        result = parse_attachment_evidence_uri_strict(
            f"memory://attachments/my_namespace/123/{VALID_SHA256}"
        )

        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_LEGACY_FORMAT
        assert "旧格式" in result.error_message
        assert "三段路径" in result.error_message

    def test_error_legacy_format_more_segments(self):
        """测试错误码: E_LEGACY_FORMAT（超过三段的路径）"""
        result = parse_attachment_evidence_uri_strict(
            f"memory://attachments/ns/sub/123/{VALID_SHA256}"
        )

        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_LEGACY_FORMAT

    def test_error_invalid_id_not_integer(self):
        """测试错误码: E_INVALID_ID（attachment_id 非整数）"""
        result = parse_attachment_evidence_uri_strict(f"memory://attachments/abc/{VALID_SHA256}")

        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_INVALID_ID
        assert "整数" in result.error_message
        assert "abc" in result.error_message

    def test_error_invalid_sha256_length(self):
        """测试错误码: E_INVALID_SHA256（sha256 长度不对）"""
        result = parse_attachment_evidence_uri_strict("memory://attachments/12345/abc123")

        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_INVALID_SHA256
        assert "64 位" in result.error_message

    def test_error_invalid_sha256_chars(self):
        """测试错误码: E_INVALID_SHA256（sha256 包含非法字符）"""
        invalid_sha = "g" * 64  # 'g' 不是十六进制字符
        result = parse_attachment_evidence_uri_strict(f"memory://attachments/12345/{invalid_sha}")

        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_INVALID_SHA256

    def test_error_malformed_missing_sha256(self):
        """测试错误码: E_MALFORMED（路径缺少 sha256）"""
        result = parse_attachment_evidence_uri_strict("memory://attachments/12345")

        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_MALFORMED

    def test_error_malformed_only_attachments(self):
        """测试错误码: E_MALFORMED（只有 attachments 前缀）"""
        result = parse_attachment_evidence_uri_strict("memory://attachments")

        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_MALFORMED

    def test_repr_success(self):
        """测试成功结果的 __repr__"""
        result = parse_attachment_evidence_uri_strict(f"memory://attachments/12345/{VALID_SHA256}")
        repr_str = repr(result)
        assert "success=True" in repr_str
        assert "12345" in repr_str

    def test_repr_failure(self):
        """测试失败结果的 __repr__"""
        result = parse_attachment_evidence_uri_strict("memory://attachments/abc/sha")
        repr_str = repr(result)
        assert "success=False" in repr_str
        assert "E_INVALID_ID" in repr_str


class TestAttachmentUriRoundTrip:
    """测试 build <-> parse 往返一致性"""

    def test_roundtrip_consistency(self):
        """测试 build 后 parse 应能正确还原"""
        attachment_id = 99999
        sha256 = VALID_SHA256

        # build
        uri = build_attachment_evidence_uri(attachment_id, sha256)

        # parse
        result = parse_attachment_evidence_uri_strict(uri)

        assert result.success is True
        assert result.attachment_id == attachment_id
        assert result.sha256 == sha256

    def test_roundtrip_with_different_ids(self):
        """测试不同 attachment_id 的往返一致性"""
        for aid in [0, 1, 100, 99999999]:
            uri = build_attachment_evidence_uri(aid, VALID_SHA256)
            result = parse_attachment_evidence_uri_strict(uri)

            assert result.success is True
            assert result.attachment_id == aid

    def test_logbook_can_parse_gateway_uri(self):
        """契约测试: Logbook 可解析 Gateway 构建的 attachment URI

        对应 03_memory_contract.md 中的要求:
        - attachment URI 格式必须严格遵循 Logbook parse_attachment_evidence_uri() 的解析规则
        - Gateway → Logbook：通过 parse_attachment_evidence_uri(uri) 解析出 attachment_id
        """
        # 模拟 Gateway 构建的 URI（符合规范格式）
        gateway_uri = f"memory://attachments/12345/{VALID_SHA256}"

        # Logbook 解析
        result = parse_attachment_evidence_uri_strict(gateway_uri)

        assert result.success is True, (
            f"Logbook 应能解析 Gateway 构建的 URI: {result.error_message}"
        )
        assert result.attachment_id == 12345
        assert result.sha256 == VALID_SHA256


class TestAttachmentUriCompatibilityMatrix:
    """
    旧格式兼容性矩阵测试

    覆盖 03_memory_contract.md 中的规范:
    - 正确格式: memory://attachments/<attachment_id>/<sha256>
    - 禁止使用旧格式: ~~memory://attachments/<namespace>/<id>/<sha256>~~
    """

    # 参数化测试: (uri, should_succeed, expected_error_code, description)
    COMPATIBILITY_CASES = [
        # ===== 正确格式: 应该成功 =====
        (f"memory://attachments/1/{VALID_SHA256}", True, None, "最小 ID"),
        (f"memory://attachments/12345/{VALID_SHA256}", True, None, "普通 ID"),
        (f"memory://attachments/9999999999/{VALID_SHA256}", True, None, "大 ID"),
        # ===== 旧格式（三段路径）: 应该被拒绝 =====
        (
            f"memory://attachments/namespace/123/{VALID_SHA256}",
            False,
            ATTACHMENT_URI_ERR_LEGACY_FORMAT,
            "旧格式: namespace/id/sha",
        ),
        (
            f"memory://attachments/scm/456/{VALID_SHA256}",
            False,
            ATTACHMENT_URI_ERR_LEGACY_FORMAT,
            "旧格式: scm/id/sha",
        ),
        (
            f"memory://attachments/team_x/789/{VALID_SHA256}",
            False,
            ATTACHMENT_URI_ERR_LEGACY_FORMAT,
            "旧格式: team_x/id/sha",
        ),
        # ===== 错误格式 =====
        (
            f"memory://attachments/abc/{VALID_SHA256}",
            False,
            ATTACHMENT_URI_ERR_INVALID_ID,
            "非整数 ID",
        ),
        (
            "memory://attachments/12345/short",
            False,
            ATTACHMENT_URI_ERR_INVALID_SHA256,
            "sha256 太短",
        ),
        ("memory://attachments/12345", False, ATTACHMENT_URI_ERR_MALFORMED, "缺少 sha256"),
        (
            f"https://attachments/12345/{VALID_SHA256}",
            False,
            ATTACHMENT_URI_ERR_NOT_MEMORY,
            "非 memory scheme",
        ),
        (
            f"memory://other/12345/{VALID_SHA256}",
            False,
            ATTACHMENT_URI_ERR_NOT_ATTACHMENTS,
            "非 attachments 路径",
        ),
    ]

    @pytest.mark.parametrize(
        "uri,should_succeed,expected_error_code,description", COMPATIBILITY_CASES
    )
    def test_attachment_uri_compatibility(
        self, uri, should_succeed, expected_error_code, description
    ):
        """参数化测试: attachment URI 格式兼容性"""
        result = parse_attachment_evidence_uri_strict(uri)

        assert result.success == should_succeed, (
            f"[{description}] expected success={should_succeed}, got {result.success}: {result.error_message}"
        )

        if not should_succeed:
            assert result.error_code == expected_error_code, (
                f"[{description}] expected error_code={expected_error_code}, got {result.error_code}"
            )


# ---------- S3 URI 审计测试 ----------

from unittest.mock import patch


class TestAuditS3UriResolution:
    """测试 ArtifactAuditor._get_store_for_uri 对 S3 URI 的处理"""

    def test_s3_uri_bucket_matches_config(self, monkeypatch):
        """测试 S3 URI bucket 与配置 bucket 一致时正常返回 key"""
        # 设置环境变量
        monkeypatch.setenv("ENGRAM_S3_BUCKET", "my-artifacts")
        monkeypatch.setenv("ENGRAM_S3_ENDPOINT", "https://s3.example.com")
        monkeypatch.setenv("ENGRAM_S3_ACCESS_KEY", "test-key")
        monkeypatch.setenv("ENGRAM_S3_SECRET_KEY", "test-secret")

        from engram.logbook.artifact_store import ObjectStore
        from scripts.artifact_audit import ArtifactAuditor

        auditor = ArtifactAuditor()

        # Mock ObjectStore 避免实际 S3 连接
        with patch.object(auditor, "_object_store", None):
            store, resolved_uri = auditor._get_store_for_uri(
                "s3://my-artifacts/path/to/object.diff"
            )

        # 验证返回的是 ObjectStore 实例
        assert isinstance(store, ObjectStore)
        # 验证返回的是 key 而不是完整 URI
        assert resolved_uri == "path/to/object.diff"

    def test_s3_uri_bucket_mismatch_raises_error(self, monkeypatch):
        """测试 S3 URI bucket 与配置 bucket 不一致时抛出错误"""
        # 设置环境变量
        monkeypatch.setenv("ENGRAM_S3_BUCKET", "my-artifacts")

        from engram.logbook.artifact_store import ArtifactReadError
        from scripts.artifact_audit import ArtifactAuditor

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
        import engram_logbook.config as config_module

        config_module._global_config = None
        config_module._global_app_config = None

        from engram.logbook.artifact_store import ArtifactReadError
        from scripts.artifact_audit import ArtifactAuditor

        auditor = ArtifactAuditor()

        with pytest.raises(ArtifactReadError) as exc_info:
            auditor._get_store_for_uri("s3://some-bucket/path/to/object.diff")

        assert "未配置 bucket" in str(exc_info.value)

    def test_s3_uri_missing_key_raises_error(self, monkeypatch):
        """测试 S3 URI 缺少 key 时抛出错误"""
        monkeypatch.setenv("ENGRAM_S3_BUCKET", "my-artifacts")

        from engram.logbook.artifact_store import ArtifactReadError
        from scripts.artifact_audit import ArtifactAuditor

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

        from engram.logbook.artifact_store import ObjectStore
        from scripts.artifact_audit import ArtifactAuditor

        auditor = ArtifactAuditor()

        with patch.object(auditor, "_object_store", None):
            store, resolved_uri = auditor._get_store_for_uri(
                "s3://engram-artifacts/scm/project/repo/git/abc123/sha256.diff"
            )

        assert isinstance(store, ObjectStore)
        assert resolved_uri == "scm/project/repo/git/abc123/sha256.diff"


# ---------- 运行测试的入口 ----------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
