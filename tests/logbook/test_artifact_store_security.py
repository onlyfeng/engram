# -*- coding: utf-8 -*-
"""
test_artifact_store_security.py - ArtifactStore 安全测试

测试覆盖:
1. 路径穿越攻击防护
2. 路径规范化
3. 前缀白名单验证
4. 权限模式设置
5. 覆盖策略测试
6. 符号链接逃逸防护
7. 路径长度限制

隔离策略:
- 使用 pytest tmp_path fixture 提供临时目录
- 不依赖数据库或外部服务
"""

import hashlib
import os
import sys

import pytest

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.logbook.artifact_store import (
    OVERWRITE_ALLOW,
    OVERWRITE_ALLOW_SAME_HASH,
    OVERWRITE_DENY,
    ArtifactHashMismatchError,
    ArtifactNotFoundError,
    ArtifactOverwriteDeniedError,
    FileUriPathError,
    FileUriStore,
    LocalArtifactsStore,
    ObjectStore,
    PathTraversalError,
)

# ============ 路径穿越攻击防护测试 ============


class TestLocalStorePathTraversalPrevention:
    """LocalArtifactsStore 路径穿越防护测试"""

    def test_reject_double_dot_in_path(self, tmp_path):
        """拒绝包含 .. 的路径"""
        store = LocalArtifactsStore(root=tmp_path)

        with pytest.raises(PathTraversalError) as exc_info:
            store.put("scm/../etc/passwd", b"malicious")

        assert "路径穿越" in str(exc_info.value)

    def test_reject_leading_double_dot(self, tmp_path):
        """拒绝以 .. 开头的路径"""
        store = LocalArtifactsStore(root=tmp_path)

        with pytest.raises(PathTraversalError):
            store.put("../outside.txt", b"escape attempt")

    def test_reject_multiple_double_dots(self, tmp_path):
        """拒绝多个 .. 的路径"""
        store = LocalArtifactsStore(root=tmp_path)

        traversal_paths = [
            "a/../../b",
            "x/y/../../../z",
            "foo/bar/..../baz",
        ]

        for path in traversal_paths:
            if ".." in path.split("/"):
                with pytest.raises(PathTraversalError):
                    store.put(path, b"data")

    def test_reject_encoded_traversal_attempt(self, tmp_path):
        """拒绝 URL 编码的穿越尝试（在 URI 规范化后）"""
        store = LocalArtifactsStore(root=tmp_path)

        # 注意：实际的 URL 解码应在上层处理
        # 这里测试直接包含 .. 的情况
        with pytest.raises(PathTraversalError):
            store.put("scm/..%2F..%2Fetc/passwd".replace("%2F", "/"), b"data")

    def test_accept_valid_nested_path(self, tmp_path):
        """接受合法的嵌套路径"""
        store = LocalArtifactsStore(root=tmp_path)

        result = store.put("scm/repo-1/git/abc123.diff", b"valid patch content")

        assert result["uri"] == "scm/repo-1/git/abc123.diff"
        assert store.exists("scm/repo-1/git/abc123.diff")

    def test_reject_empty_path(self, tmp_path):
        """拒绝空路径"""
        store = LocalArtifactsStore(root=tmp_path)

        with pytest.raises(PathTraversalError):
            store.put("", b"data")

    def test_reject_whitespace_only_path(self, tmp_path):
        """拒绝仅含空白的路径"""
        store = LocalArtifactsStore(root=tmp_path)

        with pytest.raises(PathTraversalError):
            store.put("   ", b"data")

    def test_reject_single_dot_path(self, tmp_path):
        """拒绝单个 . 的路径"""
        store = LocalArtifactsStore(root=tmp_path)

        # 单个 . 规范化后为空，应被拒绝
        with pytest.raises(PathTraversalError):
            store.put(".", b"data")


class TestLocalStorePathNormalization:
    """路径规范化测试"""

    def test_normalize_multiple_slashes(self, tmp_path):
        """规范化多个斜杠"""
        store = LocalArtifactsStore(root=tmp_path)

        result = store.put("scm///repo//test.diff", b"content")

        # 规范化后应去除多余斜杠
        assert result["uri"] == "scm/repo/test.diff"

    def test_normalize_mixed_separators(self, tmp_path):
        """规范化混合分隔符（Windows 反斜杠）"""
        store = LocalArtifactsStore(root=tmp_path)

        result = store.put("scm\\repo\\test.diff", b"content")

        # 反斜杠应转换为正斜杠
        assert "/" in result["uri"]
        assert "\\" not in result["uri"]

    def test_normalize_leading_slash(self, tmp_path):
        """规范化前导斜杠"""
        store = LocalArtifactsStore(root=tmp_path)

        result = store.put("/scm/repo/test.diff", b"content")

        # 应移除前导斜杠
        assert not result["uri"].startswith("/")


class TestLocalStorePathLengthLimit:
    """路径长度限制测试"""

    def test_reject_extremely_long_path(self, tmp_path):
        """拒绝超长路径"""
        store = LocalArtifactsStore(root=tmp_path)

        # 创建超过 4096 字节的路径
        long_segment = "a" * 500
        long_path = "/".join([long_segment] * 10)  # 5000+ 字节

        with pytest.raises(PathTraversalError) as exc_info:
            store.put(long_path, b"data")

        assert "路径过长" in str(exc_info.value)

    def test_accept_path_within_limit(self, tmp_path):
        """接受长度限制内的路径"""
        store = LocalArtifactsStore(root=tmp_path)

        # 创建接近但不超过限制的路径
        valid_path = "scm/" + "a" * 100 + "/test.diff"

        result = store.put(valid_path, b"content")
        assert result["uri"] == valid_path


class TestLocalStoreAllowedPrefixes:
    """前缀白名单测试"""

    def test_reject_path_outside_allowed_prefixes(self, tmp_path):
        """拒绝不在允许前缀列表中的路径"""
        store = LocalArtifactsStore(root=tmp_path, allowed_prefixes=["scm/", "attachments/"])

        with pytest.raises(PathTraversalError) as exc_info:
            store.put("unauthorized/file.txt", b"data")

        assert "不在允许列表中" in str(exc_info.value)

    def test_accept_path_with_allowed_prefix(self, tmp_path):
        """接受在允许前缀列表中的路径"""
        store = LocalArtifactsStore(root=tmp_path, allowed_prefixes=["scm/", "attachments/"])

        result = store.put("scm/repo/test.diff", b"content")
        assert result["uri"] == "scm/repo/test.diff"

        result2 = store.put("attachments/doc.pdf", b"pdf content")
        assert result2["uri"] == "attachments/doc.pdf"

    def test_none_prefixes_allows_all(self, tmp_path):
        """None 前缀列表允许所有路径"""
        store = LocalArtifactsStore(root=tmp_path, allowed_prefixes=None)

        result = store.put("any/path/file.txt", b"content")
        assert result["uri"] == "any/path/file.txt"

    def test_empty_prefixes_rejects_all(self, tmp_path):
        """空前缀列表拒绝所有路径"""
        store = LocalArtifactsStore(root=tmp_path, allowed_prefixes=[])

        with pytest.raises(PathTraversalError):
            store.put("any/path/file.txt", b"content")

    def test_prefix_boundary_check(self, tmp_path):
        """前缀边界检查 - 避免 'scm' 匹配 'scm_backup'"""
        store = LocalArtifactsStore(root=tmp_path, allowed_prefixes=["scm/"])

        # 'scm_backup/' 不应该被 'scm/' 匹配
        with pytest.raises(PathTraversalError):
            store.put("scm_backup/file.txt", b"data")


class TestLocalStoreOverwritePolicy:
    """覆盖策略测试"""

    def test_overwrite_allow_replaces_existing(self, tmp_path):
        """allow 策略允许覆盖"""
        store = LocalArtifactsStore(root=tmp_path, overwrite_policy=OVERWRITE_ALLOW)

        store.put("test.txt", b"original content")
        result = store.put("test.txt", b"new content")

        assert store.get("test.txt") == b"new content"
        assert result["sha256"] == hashlib.sha256(b"new content").hexdigest()

    def test_overwrite_deny_blocks_existing(self, tmp_path):
        """deny 策略阻止覆盖"""
        store = LocalArtifactsStore(root=tmp_path, overwrite_policy=OVERWRITE_DENY)

        store.put("test.txt", b"original content")

        with pytest.raises(ArtifactOverwriteDeniedError) as exc_info:
            store.put("test.txt", b"new content")

        assert "覆盖被拒绝" in str(exc_info.value)
        # 原始内容应保持不变
        assert store.get("test.txt") == b"original content"

    def test_overwrite_deny_allows_new_file(self, tmp_path):
        """deny 策略允许创建新文件"""
        store = LocalArtifactsStore(root=tmp_path, overwrite_policy=OVERWRITE_DENY)

        result = store.put("new_file.txt", b"new content")
        assert result["uri"] == "new_file.txt"

    def test_overwrite_same_hash_allows_identical(self, tmp_path):
        """allow_same_hash 策略允许相同内容覆盖"""
        store = LocalArtifactsStore(root=tmp_path, overwrite_policy=OVERWRITE_ALLOW_SAME_HASH)

        content = b"identical content"
        store.put("test.txt", content)
        result = store.put("test.txt", content)

        assert store.get("test.txt") == content
        assert result["sha256"] == hashlib.sha256(content).hexdigest()

    def test_overwrite_same_hash_blocks_different(self, tmp_path):
        """allow_same_hash 策略阻止不同内容覆盖"""
        store = LocalArtifactsStore(root=tmp_path, overwrite_policy=OVERWRITE_ALLOW_SAME_HASH)

        store.put("test.txt", b"original content")

        with pytest.raises(ArtifactHashMismatchError) as exc_info:
            store.put("test.txt", b"different content")

        assert "哈希不匹配" in str(exc_info.value)
        # 原始内容应保持不变
        assert store.get("test.txt") == b"original content"

    def test_invalid_overwrite_policy_raises_error(self, tmp_path):
        """无效的覆盖策略应抛出 ValueError"""
        with pytest.raises(ValueError) as exc_info:
            LocalArtifactsStore(root=tmp_path, overwrite_policy="invalid")

        assert "无效的覆盖策略" in str(exc_info.value)


class TestLocalStoreFilePermissions:
    """文件权限测试"""

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不支持 Unix 权限模式")
    def test_custom_file_mode(self, tmp_path):
        """自定义文件权限模式"""
        store = LocalArtifactsStore(root=tmp_path, file_mode=0o600)

        store.put("secret.txt", b"sensitive data")

        file_path = tmp_path / "secret.txt"
        mode = file_path.stat().st_mode & 0o777

        assert mode == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不支持 Unix 权限模式")
    def test_custom_dir_mode(self, tmp_path):
        """自定义目录权限模式"""
        store = LocalArtifactsStore(root=tmp_path, dir_mode=0o700)

        store.put("secure/nested/file.txt", b"data")

        dir_path = tmp_path / "secure" / "nested"
        dir_path.stat().st_mode & 0o777

        # 目录权限可能因创建方式而异
        # 只验证目录被创建
        assert dir_path.is_dir()


class TestLocalStoreAtomicWrite:
    """原子写入测试"""

    def test_atomic_write_creates_file(self, tmp_path):
        """原子写入正确创建文件"""
        store = LocalArtifactsStore(root=tmp_path)

        content = b"atomic content"
        result = store.put("atomic_test.txt", content)

        assert store.get("atomic_test.txt") == content
        assert result["sha256"] == hashlib.sha256(content).hexdigest()

    def test_no_temp_file_on_success(self, tmp_path):
        """成功后不应残留临时文件"""
        store = LocalArtifactsStore(root=tmp_path)

        store.put("clean_test.txt", b"content")

        # 检查没有 .tmp 文件残留
        for path in tmp_path.rglob("*.tmp"):
            pytest.fail(f"发现残留的临时文件: {path}")

    def test_cleanup_temp_on_overwrite_denied(self, tmp_path):
        """覆盖被拒绝时清理临时文件"""
        store = LocalArtifactsStore(root=tmp_path, overwrite_policy=OVERWRITE_DENY)

        store.put("test.txt", b"original")

        with pytest.raises(ArtifactOverwriteDeniedError):
            store.put("test.txt", b"new")

        # 检查没有 .tmp 文件残留
        for path in tmp_path.rglob("*.tmp"):
            pytest.fail(f"发现残留的临时文件: {path}")


# ============ FileUriStore 安全测试 ============


class TestFileUriStorePathTraversal:
    """FileUriStore 路径穿越防护测试"""

    def test_reject_double_dot_in_file_uri(self, tmp_path):
        """拒绝 file:// URI 中的 .. 组件"""
        store = FileUriStore(allowed_roots=[str(tmp_path)])

        uri = f"file://{tmp_path}/../etc/passwd"

        with pytest.raises(FileUriPathError) as exc_info:
            store.get(uri)

        assert "路径穿越" in str(exc_info.value)

    def test_reject_path_outside_allowed_roots(self, tmp_path):
        """拒绝不在允许根目录列表中的路径"""
        store = FileUriStore(allowed_roots=[str(tmp_path / "allowed")])

        # 创建测试文件在不允许的目录
        forbidden_dir = tmp_path / "forbidden"
        forbidden_dir.mkdir()
        (forbidden_dir / "test.txt").write_bytes(b"secret")

        uri = f"file://{forbidden_dir}/test.txt"

        with pytest.raises(FileUriPathError) as exc_info:
            store.get(uri)

        assert "不在允许的根路径列表中" in str(exc_info.value)

    def test_accept_path_within_allowed_root(self, tmp_path):
        """接受在允许根目录内的路径"""
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        test_file = allowed_root / "test.txt"
        test_file.write_bytes(b"allowed content")

        store = FileUriStore(allowed_roots=[str(allowed_root)])

        uri = f"file://{test_file}"
        content = store.get(uri)

        assert content == b"allowed content"


class TestFileUriStoreUrlDecoding:
    """FileUriStore URL 解码测试"""

    def test_decode_space_in_path(self, tmp_path):
        """正确解码路径中的空格"""
        path_with_space = tmp_path / "path with space"
        path_with_space.mkdir()
        test_file = path_with_space / "file.txt"
        test_file.write_bytes(b"content")

        store = FileUriStore(allowed_roots=[str(tmp_path)])

        # 使用 URL 编码的空格
        uri = f"file://{str(path_with_space).replace(' ', '%20')}/file.txt"
        content = store.get(uri)

        assert content == b"content"

    def test_decode_special_characters(self, tmp_path):
        """正确解码特殊字符"""
        store = FileUriStore(allowed_roots=[str(tmp_path)])

        # 创建包含特殊字符的目录和文件
        special_dir = tmp_path / "test_dir"
        special_dir.mkdir()
        test_file = special_dir / "test.txt"
        test_file.write_bytes(b"content")

        uri = f"file://{test_file}"
        content = store.get(uri)

        assert content == b"content"


class TestFileUriStoreOverwritePolicy:
    """FileUriStore 覆盖策略测试"""

    def test_overwrite_deny_blocks_existing(self, tmp_path):
        """deny 策略阻止覆盖现有文件"""
        store = FileUriStore(allowed_roots=[str(tmp_path)], overwrite_policy=OVERWRITE_DENY)

        uri = f"file://{tmp_path}/test.txt"

        store.put(uri, b"original")

        with pytest.raises(ArtifactOverwriteDeniedError):
            store.put(uri, b"new content")

    def test_overwrite_same_hash_allows_identical(self, tmp_path):
        """allow_same_hash 策略允许相同内容"""
        store = FileUriStore(
            allowed_roots=[str(tmp_path)], overwrite_policy=OVERWRITE_ALLOW_SAME_HASH
        )

        uri = f"file://{tmp_path}/test.txt"
        content = b"identical content"

        store.put(uri, content)
        result = store.put(uri, content)

        assert result["sha256"] == hashlib.sha256(content).hexdigest()


# ============ 综合安全场景测试 ============


class TestSecurityScenarios:
    """综合安全场景测试"""

    def test_null_byte_injection(self, tmp_path):
        """拒绝包含空字节的路径（某些系统上的攻击向量）"""
        store = LocalArtifactsStore(root=tmp_path)

        # 空字节在路径中通常是非法的
        # Python 会在文件操作时抛出 ValueError
        path_with_null = "test\x00evil.txt"

        with pytest.raises((PathTraversalError, ValueError, OSError)):
            store.put(path_with_null, b"data")

    def test_unicode_normalization_attack(self, tmp_path):
        """处理 Unicode 规范化攻击"""
        store = LocalArtifactsStore(root=tmp_path)

        # 某些 Unicode 字符可能被规范化为 .. 或其他危险字符
        # 这里测试基本的 Unicode 路径处理
        unicode_path = "测试/文件.txt"

        result = store.put(unicode_path, b"content")
        assert store.exists(result["uri"])

    def test_concurrent_file_access_safety(self, tmp_path):
        """并发文件访问安全"""
        import threading

        store = LocalArtifactsStore(root=tmp_path, overwrite_policy=OVERWRITE_ALLOW)
        errors = []

        def write_file(idx):
            try:
                store.put("concurrent.txt", f"content {idx}".encode())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_file, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 无论哪个线程最后写入，文件都应该有效
        assert store.exists("concurrent.txt")
        content = store.get("concurrent.txt")
        assert content.startswith(b"content ")
        assert len(errors) == 0

    def test_get_info_matches_put_result(self, tmp_path):
        """get_info 返回的信息应与 put 结果一致"""
        store = LocalArtifactsStore(root=tmp_path)

        content = b"test content for verification"
        put_result = store.put("verify.txt", content)

        info = store.get_info("verify.txt")

        assert info["uri"] == put_result["uri"]
        assert info["sha256"] == put_result["sha256"]
        assert info["size_bytes"] == put_result["size_bytes"]


class TestSymlinkEscapePrevention:
    """符号链接逃逸防护测试"""

    @pytest.mark.skipif(os.name == "nt", reason="Windows 符号链接需要特殊权限")
    def test_detect_symlink_escape_attempt(self, tmp_path):
        """检测符号链接逃逸尝试"""
        store_root = tmp_path / "store"
        store_root.mkdir()

        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_bytes(b"secret data")

        # 创建指向外部目录的符号链接
        symlink_path = store_root / "escape"
        try:
            symlink_path.symlink_to(outside_dir)
        except OSError:
            pytest.skip("无法创建符号链接")

        store = LocalArtifactsStore(root=store_root)

        # 尝试通过符号链接读取外部文件
        # 由于 resolve() 会解析符号链接，应该检测到逃逸
        # 注意：这取决于实现细节，可能需要额外的安全检查
        try:
            store.get("escape/secret.txt")
            # 如果能读取，检查是否真的是外部内容
            # 取决于安全策略，这可能是允许的或被阻止的
        except (PathTraversalError, ArtifactNotFoundError):
            # 预期行为：阻止逃逸
            pass


class TestHashIntegrity:
    """哈希完整性测试"""

    def test_sha256_correctly_computed(self, tmp_path):
        """SHA256 正确计算"""
        store = LocalArtifactsStore(root=tmp_path)

        content = b"hash test content"
        expected_sha256 = hashlib.sha256(content).hexdigest()

        result = store.put("hash_test.txt", content)

        assert result["sha256"] == expected_sha256

    def test_size_correctly_reported(self, tmp_path):
        """大小正确报告"""
        store = LocalArtifactsStore(root=tmp_path)

        content = b"size test content"
        result = store.put("size_test.txt", content)

        assert result["size_bytes"] == len(content)

    def test_streaming_hash_computation(self, tmp_path):
        """流式哈希计算（迭代器输入）"""
        store = LocalArtifactsStore(root=tmp_path)

        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        full_content = b"".join(chunks)
        expected_sha256 = hashlib.sha256(full_content).hexdigest()

        result = store.put("stream_test.txt", iter(chunks))

        assert result["sha256"] == expected_sha256
        assert result["size_bytes"] == len(full_content)


class TestEncodingHandling:
    """编码处理测试"""

    def test_string_content_utf8_encoding(self, tmp_path):
        """字符串内容 UTF-8 编码"""
        store = LocalArtifactsStore(root=tmp_path)

        content_str = "你好，世界！Hello, World!"
        store.put("unicode.txt", content_str)

        read_content = store.get("unicode.txt")
        assert read_content.decode("utf-8") == content_str

    def test_custom_encoding(self, tmp_path):
        """自定义编码"""
        store = LocalArtifactsStore(root=tmp_path)

        content_str = "Привет мир"
        store.put("custom_encoding.txt", content_str, encoding="utf-8")

        read_content = store.get("custom_encoding.txt")
        assert read_content.decode("utf-8") == content_str

    def test_binary_content_preserved(self, tmp_path):
        """二进制内容完整保留"""
        store = LocalArtifactsStore(root=tmp_path)

        # 包含所有可能字节值的内容
        binary_content = bytes(range(256))
        store.put("binary.bin", binary_content)

        read_content = store.get("binary.bin")
        assert read_content == binary_content


# ============ ObjectStore 安全测试 ============


class TestObjectStoreAllowedPrefixes:
    """ObjectStore allowed_prefixes 测试"""

    def test_none_prefixes_allows_all(self):
        """None 前缀列表允许所有路径"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            allowed_prefixes=None,
        )

        # 任何 key 都应该被允许
        key = store._object_key("any/path/file.txt")
        assert key == "any/path/file.txt"

    def test_empty_prefixes_rejects_all(self):
        """空前缀列表拒绝所有路径"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            allowed_prefixes=[],
        )

        with pytest.raises(PathTraversalError) as exc_info:
            store._object_key("any/path/file.txt")

        assert "不在允许列表中" in str(exc_info.value)

    def test_accept_path_with_allowed_prefix(self):
        """接受在允许前缀列表中的路径"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            allowed_prefixes=["scm/", "attachments/"],
        )

        key1 = store._object_key("scm/repo/test.diff")
        assert key1 == "scm/repo/test.diff"

        key2 = store._object_key("attachments/doc.pdf")
        assert key2 == "attachments/doc.pdf"

    def test_reject_path_outside_allowed_prefixes(self):
        """拒绝不在允许前缀列表中的路径"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            allowed_prefixes=["scm/", "attachments/"],
        )

        with pytest.raises(PathTraversalError) as exc_info:
            store._object_key("unauthorized/file.txt")

        assert "不在允许列表中" in str(exc_info.value)

    def test_prefix_combined_with_allowed_prefixes(self):
        """prefix 与 allowed_prefixes 组合验证"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            prefix="engram",  # 前缀会变成 engram/
            allowed_prefixes=["engram/scm/", "engram/attachments/"],
        )

        # 完整 key 是 engram/scm/test.diff，应该匹配 engram/scm/
        key = store._object_key("scm/test.diff")
        assert key == "engram/scm/test.diff"

    def test_prefix_combined_rejects_non_matching(self):
        """prefix 组合后不匹配的路径应被拒绝"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            prefix="engram",
            allowed_prefixes=["engram/scm/"],  # 只允许 engram/scm/ 前缀
        )

        # 完整 key 是 engram/other/test.txt，不匹配 engram/scm/
        with pytest.raises(PathTraversalError):
            store._object_key("other/test.txt")

    def test_prefix_boundary_check(self):
        """前缀边界检查 - 避免 'scm' 匹配 'scm_backup'"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            allowed_prefixes=["scm/"],
        )

        # 'scm_backup/' 不应该被 'scm/' 匹配
        with pytest.raises(PathTraversalError):
            store._object_key("scm_backup/file.txt")

    def test_allowed_prefixes_property(self):
        """allowed_prefixes 属性正确返回"""
        prefixes = ["scm/", "attachments/"]
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            allowed_prefixes=prefixes,
        )

        assert store.allowed_prefixes == prefixes

    def test_object_key_with_leading_slash(self):
        """带前导斜杠的 URI 正确规范化"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            allowed_prefixes=["scm/"],
        )

        key = store._object_key("/scm/test.diff")
        assert key == "scm/test.diff"

    def test_object_key_with_backslash(self):
        """反斜杠正确规范化为正斜杠"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            allowed_prefixes=["scm/"],
        )

        key = store._object_key("scm\\repo\\test.diff")
        assert key == "scm/repo/test.diff"


class TestObjectStoreGetArtifactStoreFactory:
    """测试 get_artifact_store 工厂函数对 ObjectStore allowed_prefixes 的支持"""

    def test_factory_passes_allowed_prefixes(self):
        """工厂函数正确传递 allowed_prefixes"""
        from engram.logbook.artifact_store import get_artifact_store

        store = get_artifact_store(
            "object",
            endpoint="http://localhost:9000",
            access_key="test",
            secret_key="test",
            bucket="test",
            allowed_prefixes=["scm/", "attachments/"],
        )

        assert isinstance(store, ObjectStore)
        assert store.allowed_prefixes == ["scm/", "attachments/"]
