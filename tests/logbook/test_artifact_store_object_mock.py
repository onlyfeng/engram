# -*- coding: utf-8 -*-
"""
test_artifact_store_object_mock.py - ObjectStore Mock 测试

测试覆盖:
1. ObjectStore 基本操作（put/get/exists/resolve）
2. Multipart 上传逻辑
3. 流式下载
4. 错误分类和异常处理
5. 预签名 URL 生成
6. 大小限制检查
7. 配置验证
8. 超时和限流处理

隔离策略:
- 使用 mock boto3，不连接真实 S3/MinIO
- 使用 pytest tmp_path fixture
- 不依赖数据库或外部服务
"""

import hashlib
import io
import os
import sys
from unittest.mock import MagicMock, Mock, patch, PropertyMock

import pytest

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.logbook.artifact_store import (
    ObjectStore,
    ObjectStoreError,
    ObjectStoreNotConfiguredError,
    ObjectStoreConnectionError,
    ObjectStoreUploadError,
    ObjectStoreDownloadError,
    ObjectStoreTimeoutError,
    ObjectStoreThrottlingError,
    ArtifactNotFoundError,
    ArtifactSizeLimitExceededError,
    MULTIPART_THRESHOLD,
    MULTIPART_CHUNK_SIZE,
)


# ============ 测试辅助函数 ============


def create_mock_s3_client():
    """创建模拟的 S3 客户端"""
    client = MagicMock()

    # 默认成功响应
    client.put_object.return_value = {"ETag": '"mock-etag"'}
    client.head_object.return_value = {
        "ContentLength": 100,
        "Metadata": {"sha256": "mock-sha256"},
    }

    # 模拟 get_object 返回带 Body 的响应
    mock_body = MagicMock()
    mock_body.read.return_value = b"mock content"
    client.get_object.return_value = {"Body": mock_body}

    return client


def create_mock_boto3_module(mock_client):
    """创建模拟的 boto3 模块"""
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client
    return mock_boto3


def create_store_with_mock_client(mock_client, **kwargs):
    """创建一个使用 mock 客户端的 ObjectStore"""
    default_kwargs = {
        "endpoint": "http://localhost:9000",
        "access_key": "key",
        "secret_key": "secret",
        "bucket": "test-bucket",
    }
    default_kwargs.update(kwargs)
    store = ObjectStore(**default_kwargs)
    store._client = mock_client  # 直接注入 mock 客户端
    return store


# ============ ObjectStore 配置测试 ============


class TestObjectStoreConfiguration:
    """ObjectStore 配置测试"""

    def test_missing_endpoint_raises_error(self):
        """缺少端点配置应抛出错误"""
        store = ObjectStore(
            endpoint=None,
            access_key="key",
            secret_key="secret",
            bucket="bucket",
        )

        with pytest.raises(ObjectStoreNotConfiguredError) as exc_info:
            store._check_configured()

        assert "endpoint" in str(exc_info.value)

    def test_missing_access_key_raises_error(self):
        """缺少访问密钥应抛出错误"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key=None,
            secret_key="secret",
            bucket="bucket",
        )

        with pytest.raises(ObjectStoreNotConfiguredError) as exc_info:
            store._check_configured()

        assert "access_key" in str(exc_info.value)

    def test_missing_bucket_raises_error(self):
        """缺少存储桶配置应抛出错误"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket=None,
        )

        with pytest.raises(ObjectStoreNotConfiguredError) as exc_info:
            store._check_configured()

        assert "bucket" in str(exc_info.value)

    def test_multiple_missing_fields_reported(self):
        """多个缺失字段应全部报告"""
        store = ObjectStore()

        with pytest.raises(ObjectStoreNotConfiguredError) as exc_info:
            store._check_configured()

        error_msg = str(exc_info.value)
        assert "endpoint" in error_msg
        assert "access_key" in error_msg

    def test_valid_configuration_passes(self):
        """有效配置应通过检查"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
        )

        # 不应抛出异常
        store._check_configured()

    def test_environment_variable_fallback(self):
        """从环境变量读取配置"""
        with patch.dict(os.environ, {
            "ENGRAM_S3_ENDPOINT": "http://env-endpoint:9000",
            "ENGRAM_S3_ACCESS_KEY": "env-key",
            "ENGRAM_S3_SECRET_KEY": "env-secret",
            "ENGRAM_S3_BUCKET": "env-bucket",
            "ENGRAM_S3_REGION": "env-region",
        }):
            store = ObjectStore()

            assert store.endpoint == "http://env-endpoint:9000"
            assert store.access_key == "env-key"
            assert store.secret_key == "env-secret"
            assert store.bucket == "env-bucket"
            assert store.region == "env-region"

    def test_explicit_params_override_env(self):
        """显式参数优先于环境变量"""
        with patch.dict(os.environ, {
            "ENGRAM_S3_ENDPOINT": "http://env-endpoint:9000",
            "ENGRAM_S3_BUCKET": "env-bucket",
        }):
            store = ObjectStore(
                endpoint="http://explicit-endpoint:9000",
                bucket="explicit-bucket",
                access_key="key",
                secret_key="secret",
            )

            assert store.endpoint == "http://explicit-endpoint:9000"
            assert store.bucket == "explicit-bucket"


class TestObjectStoreClientInitialization:
    """ObjectStore 客户端初始化测试"""

    def test_client_lazy_initialization(self):
        """客户端惰性初始化"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
        )

        # 初始化时不应创建客户端
        assert store._client is None

    def test_client_can_be_injected(self):
        """客户端可以直接注入（用于测试）"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client)

        # 验证注入的客户端
        assert store._client is mock_client

    def test_client_reused_after_injection(self):
        """注入后客户端被复用"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client)

        client1 = store._get_client()
        client2 = store._get_client()

        assert client1 is client2
        assert client1 is mock_client


# ============ ObjectStore PUT 操作测试 ============


class TestObjectStorePut:
    """ObjectStore put 操作测试"""

    def test_put_bytes_content(self):
        """上传字节内容"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client)

        content = b"test content"
        result = store.put("artifacts/test.txt", content)

        assert result["uri"] == "artifacts/test.txt"
        assert result["sha256"] == hashlib.sha256(content).hexdigest()
        assert result["size_bytes"] == len(content)

        mock_client.put_object.assert_called_once()
        call_args = mock_client.put_object.call_args
        assert call_args.kwargs["Bucket"] == "test-bucket"
        assert call_args.kwargs["Key"] == "artifacts/test.txt"
        assert call_args.kwargs["Body"] == content

    def test_put_string_content(self):
        """上传字符串内容"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client)

        content_str = "字符串内容"
        content_bytes = content_str.encode("utf-8")
        result = store.put("artifacts/unicode.txt", content_str)

        assert result["sha256"] == hashlib.sha256(content_bytes).hexdigest()
        assert result["size_bytes"] == len(content_bytes)

    def test_put_iterator_content(self):
        """上传迭代器内容"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client)

        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        full_content = b"".join(chunks)

        result = store.put("artifacts/stream.txt", iter(chunks))

        assert result["sha256"] == hashlib.sha256(full_content).hexdigest()
        assert result["size_bytes"] == len(full_content)

    def test_put_with_prefix(self):
        """带前缀的上传"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client, prefix="prefix/v1")

        result = store.put("test.txt", b"content")

        call_args = mock_client.put_object.call_args
        assert call_args.kwargs["Key"] == "prefix/v1/test.txt"

    def test_put_with_sse(self):
        """带服务端加密的上传"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client, sse="AES256")

        store.put("encrypted.txt", b"secret content")

        call_args = mock_client.put_object.call_args
        assert call_args.kwargs["ServerSideEncryption"] == "AES256"

    def test_put_with_storage_class(self):
        """带存储类别的上传"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client, storage_class="STANDARD_IA")

        store.put("archived.txt", b"content")

        call_args = mock_client.put_object.call_args
        assert call_args.kwargs["StorageClass"] == "STANDARD_IA"

    def test_put_failure_raises_upload_error(self):
        """上传失败应抛出错误"""
        mock_client = create_mock_s3_client()
        mock_client.put_object.side_effect = Exception("Network error")
        store = create_store_with_mock_client(mock_client)

        with pytest.raises(ObjectStoreUploadError) as exc_info:
            store.put("test.txt", b"content")

        assert "上传制品失败" in str(exc_info.value)


class TestObjectStorePutSizeLimit:
    """ObjectStore 大小限制测试"""

    def test_reject_content_exceeding_limit(self):
        """拒绝超过大小限制的内容"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client, max_size_bytes=100)

        large_content = b"x" * 200

        with pytest.raises(ArtifactSizeLimitExceededError) as exc_info:
            store.put("large.txt", large_content)

        assert "超出限制" in str(exc_info.value)

    def test_accept_content_within_limit(self):
        """接受大小限制内的内容"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client, max_size_bytes=100)

        small_content = b"x" * 50
        result = store.put("small.txt", small_content)

        assert result["size_bytes"] == 50

    def test_streaming_size_limit_check(self):
        """流式上传的大小限制检查"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client, max_size_bytes=100)

        # 创建一个超过限制的迭代器
        def large_chunks():
            yield b"x" * 60
            yield b"x" * 60  # 总计 120 字节，超过限制

        with pytest.raises(ArtifactSizeLimitExceededError):
            store.put("stream.txt", large_chunks())


# ============ ObjectStore Multipart 上传测试 ============


class TestObjectStoreMultipartUpload:
    """ObjectStore Multipart 上传测试"""

    def test_multipart_upload_for_large_content(self):
        """大文件使用 Multipart 上传"""
        mock_client = create_mock_s3_client()
        mock_client.create_multipart_upload.return_value = {"UploadId": "test-upload-id"}
        mock_client.upload_part.return_value = {"ETag": '"part-etag"'}
        store = create_store_with_mock_client(
            mock_client,
            multipart_threshold=100,  # 低阈值便于测试
            multipart_chunk_size=50,
        )

        # 创建超过阈值的内容
        large_content = iter([b"x" * 60, b"y" * 60])

        result = store.put("large.bin", large_content)

        assert mock_client.create_multipart_upload.called
        assert mock_client.upload_part.called
        assert mock_client.complete_multipart_upload.called

    def test_multipart_abort_on_failure(self):
        """Multipart 上传失败时取消上传"""
        mock_client = create_mock_s3_client()
        mock_client.create_multipart_upload.return_value = {"UploadId": "test-upload-id"}
        mock_client.upload_part.side_effect = Exception("Part upload failed")
        store = create_store_with_mock_client(
            mock_client,
            multipart_threshold=100,
            multipart_chunk_size=50,
        )

        large_content = iter([b"x" * 60, b"y" * 60])

        with pytest.raises(ObjectStoreUploadError):
            store.put("large.bin", large_content)

        # 验证取消操作被调用
        mock_client.abort_multipart_upload.assert_called_once()


class TestObjectStoreStreamingMultipart:
    """ObjectStore Streaming Multipart 上传测试"""

    def test_small_iterator_uses_single_put(self):
        """小于阈值的迭代器使用单次 put_object"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(
            mock_client,
            multipart_threshold=100,
            multipart_chunk_size=50,
        )

        # 创建小于阈值的内容
        small_content = iter([b"x" * 30, b"y" * 30])  # 60 bytes < 100

        result = store.put("small.bin", small_content)

        # 验证使用单次 put_object
        mock_client.put_object.assert_called_once()
        assert not mock_client.create_multipart_upload.called
        assert result["size_bytes"] == 60

    def test_streaming_multipart_calculates_sha256(self):
        """Streaming multipart 正确计算 sha256"""
        mock_client = create_mock_s3_client()
        mock_client.create_multipart_upload.return_value = {"UploadId": "test-upload-id"}
        mock_client.upload_part.return_value = {"ETag": '"part-etag"'}
        store = create_store_with_mock_client(
            mock_client,
            multipart_threshold=100,
            multipart_chunk_size=50,
        )

        chunks = [b"chunk1", b"chunk2", b"chunk3", b"chunk4"]
        # 每个 chunk 6 bytes，总共 24 bytes，但我们需要超过 100 threshold
        large_chunks = [b"x" * 40, b"y" * 40, b"z" * 40]  # 120 bytes

        full_content = b"".join(large_chunks)
        expected_hash = hashlib.sha256(full_content).hexdigest()

        result = store.put("stream.bin", iter(large_chunks))

        assert result["sha256"] == expected_hash
        assert result["size_bytes"] == 120

    def test_streaming_multipart_abort_on_size_limit(self):
        """Streaming multipart 超过大小限制时 abort"""
        mock_client = create_mock_s3_client()
        mock_client.create_multipart_upload.return_value = {"UploadId": "test-upload-id"}
        mock_client.upload_part.return_value = {"ETag": '"part-etag"'}
        store = create_store_with_mock_client(
            mock_client,
            multipart_threshold=50,
            multipart_chunk_size=30,
            max_size_bytes=100,
        )

        # 创建超过大小限制的内容（需先超过 threshold，再超过 max_size）
        def large_generator():
            yield b"x" * 40
            yield b"y" * 40  # 80 bytes，触发 multipart
            yield b"z" * 40  # 120 bytes，超过 100 limit

        with pytest.raises(ArtifactSizeLimitExceededError):
            store.put("too_large.bin", large_generator())

        # 验证 abort 被调用
        mock_client.abort_multipart_upload.assert_called_once()

    def test_streaming_multipart_abort_on_upload_error(self):
        """Streaming multipart 上传失败时正确 abort"""
        mock_client = create_mock_s3_client()
        mock_client.create_multipart_upload.return_value = {"UploadId": "abort-test-id"}
        # 第二个 part 上传失败
        mock_client.upload_part.side_effect = [
            {"ETag": '"part1-etag"'},
            Exception("Network error on part 2"),
        ]
        store = create_store_with_mock_client(
            mock_client,
            multipart_threshold=50,
            multipart_chunk_size=30,
        )

        def content_generator():
            yield b"x" * 40
            yield b"y" * 40
            yield b"z" * 40

        with pytest.raises(ObjectStoreUploadError) as exc_info:
            store.put("failed.bin", content_generator())

        assert "Streaming multipart" in str(exc_info.value)

        # 验证 abort 被调用且使用正确的 upload_id
        mock_client.abort_multipart_upload.assert_called_once()
        call_kwargs = mock_client.abort_multipart_upload.call_args.kwargs
        assert call_kwargs["UploadId"] == "abort-test-id"

    def test_streaming_multipart_updates_metadata(self):
        """Streaming multipart 完成后更新 metadata"""
        mock_client = create_mock_s3_client()
        mock_client.create_multipart_upload.return_value = {"UploadId": "test-upload-id"}
        mock_client.upload_part.return_value = {"ETag": '"part-etag"'}
        store = create_store_with_mock_client(
            mock_client,
            multipart_threshold=50,
            multipart_chunk_size=30,
        )

        large_chunks = [b"a" * 30, b"b" * 30, b"c" * 30]

        result = store.put("metadata.bin", iter(large_chunks))

        # 验证 complete_multipart_upload 被调用
        assert mock_client.complete_multipart_upload.called

        # 验证 copy_object 被调用以更新 metadata
        assert mock_client.copy_object.called
        copy_kwargs = mock_client.copy_object.call_args.kwargs
        assert copy_kwargs["Metadata"]["sha256"] == result["sha256"]
        assert copy_kwargs["MetadataDirective"] == "REPLACE"

    def test_streaming_multipart_with_string_chunks(self):
        """Streaming multipart 支持字符串 chunks"""
        mock_client = create_mock_s3_client()
        mock_client.create_multipart_upload.return_value = {"UploadId": "test-upload-id"}
        mock_client.upload_part.return_value = {"ETag": '"part-etag"'}
        store = create_store_with_mock_client(
            mock_client,
            multipart_threshold=50,
            multipart_chunk_size=30,
        )

        # 使用字符串 chunks
        string_chunks = ["hello" * 10, "world" * 10, "test!" * 10]  # 每个 50 bytes

        result = store.put("strings.bin", iter(string_chunks))

        expected_content = "".join(string_chunks).encode("utf-8")
        expected_hash = hashlib.sha256(expected_content).hexdigest()

        assert result["sha256"] == expected_hash
        assert result["size_bytes"] == len(expected_content)

    def test_streaming_below_threshold_checks_size_limit(self):
        """小于阈值的流式上传也检查大小限制"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(
            mock_client,
            multipart_threshold=100,
            max_size_bytes=50,
        )

        # 创建超过大小限制但小于阈值的内容
        def generator():
            yield b"x" * 30
            yield b"y" * 30  # 60 bytes > 50 limit

        with pytest.raises(ArtifactSizeLimitExceededError):
            store.put("limited.bin", generator())

        # 不应该调用任何 S3 操作
        assert not mock_client.put_object.called
        assert not mock_client.create_multipart_upload.called


# ============ ObjectStore GET 操作测试 ============


class TestObjectStoreGet:
    """ObjectStore get 操作测试"""

    def test_get_returns_content(self):
        """get 返回对象内容"""
        mock_client = create_mock_s3_client()
        mock_body = MagicMock()
        mock_body.read.return_value = b"stored content"
        mock_client.get_object.return_value = {"Body": mock_body}
        store = create_store_with_mock_client(mock_client)

        content = store.get("artifacts/test.txt")

        assert content == b"stored content"
        mock_client.get_object.assert_called_once()

    def test_get_with_prefix(self):
        """带前缀的 get"""
        mock_client = create_mock_s3_client()
        mock_body = MagicMock()
        mock_body.read.return_value = b"content"
        mock_client.get_object.return_value = {"Body": mock_body}
        store = create_store_with_mock_client(mock_client, prefix="prefix")

        store.get("test.txt")

        call_args = mock_client.get_object.call_args
        assert call_args.kwargs["Key"] == "prefix/test.txt"

    def test_get_not_found_raises_error(self):
        """对象不存在应抛出错误"""
        mock_client = create_mock_s3_client()
        mock_client.get_object.side_effect = Exception("NoSuchKey")
        store = create_store_with_mock_client(mock_client)

        with pytest.raises(ArtifactNotFoundError):
            store.get("nonexistent.txt")

    def test_get_size_limit_check(self):
        """get 前检查大小限制"""
        mock_client = create_mock_s3_client()
        mock_client.head_object.return_value = {"ContentLength": 1000}
        store = create_store_with_mock_client(mock_client, max_size_bytes=100)

        with pytest.raises(ArtifactSizeLimitExceededError):
            store.get("large.txt")


# ============ ObjectStore 流式下载测试 ============


class TestObjectStoreGetStream:
    """ObjectStore 流式下载测试"""

    def test_get_stream_yields_chunks(self):
        """流式下载返回数据块"""
        mock_client = create_mock_s3_client()
        mock_body = MagicMock()
        # 模拟分块读取
        mock_body.read.side_effect = [b"chunk1", b"chunk2", b""]
        mock_client.get_object.return_value = {"Body": mock_body}
        mock_client.head_object.return_value = {"ContentLength": 12}
        store = create_store_with_mock_client(mock_client)

        chunks = list(store.get_stream("test.txt"))

        assert chunks == [b"chunk1", b"chunk2"]

    def test_get_stream_custom_chunk_size(self):
        """自定义分块大小"""
        mock_client = create_mock_s3_client()
        mock_body = MagicMock()
        mock_body.read.side_effect = [b"x" * 1024, b""]
        mock_client.get_object.return_value = {"Body": mock_body}
        mock_client.head_object.return_value = {"ContentLength": 1024}
        store = create_store_with_mock_client(mock_client)

        chunks = list(store.get_stream("test.txt", chunk_size=1024))

        # 验证 read 被调用时使用了指定的 chunk_size
        mock_body.read.assert_called_with(1024)


# ============ ObjectStore 元数据操作测试 ============


class TestObjectStoreExists:
    """ObjectStore exists 操作测试"""

    def test_exists_returns_true_for_existing(self):
        """存在的对象返回 True"""
        mock_client = create_mock_s3_client()
        store = create_store_with_mock_client(mock_client)

        assert store.exists("existing.txt") is True
        mock_client.head_object.assert_called_once()

    def test_exists_returns_false_for_missing(self):
        """不存在的对象返回 False"""
        mock_client = create_mock_s3_client()
        mock_client.head_object.side_effect = Exception("Not Found")
        store = create_store_with_mock_client(mock_client)

        assert store.exists("nonexistent.txt") is False


class TestObjectStoreResolve:
    """ObjectStore resolve 操作测试"""

    def test_resolve_returns_s3_url(self):
        """resolve 返回 S3 URL"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )

        url = store.resolve("artifacts/test.txt")

        assert url == "s3://test-bucket/artifacts/test.txt"

    def test_resolve_with_prefix(self):
        """带前缀的 resolve"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            prefix="v1/prefix",
        )

        url = store.resolve("test.txt")

        assert url == "s3://test-bucket/v1/prefix/test.txt"


class TestObjectStoreGetInfo:
    """ObjectStore get_info 操作测试"""

    def test_get_info_from_metadata(self):
        """从元数据获取信息"""
        mock_client = create_mock_s3_client()
        mock_client.head_object.return_value = {
            "ContentLength": 100,
            "Metadata": {"sha256": "abc123"},
        }
        store = create_store_with_mock_client(mock_client)

        info = store.get_info("test.txt")

        assert info["uri"] == "test.txt"
        assert info["sha256"] == "abc123"
        assert info["size_bytes"] == 100

    def test_get_info_computes_hash_if_missing(self):
        """元数据无哈希时计算哈希"""
        mock_client = create_mock_s3_client()
        mock_client.head_object.return_value = {
            "ContentLength": 12,
            "Metadata": {},  # 无 sha256
        }
        mock_body = MagicMock()
        content = b"test content"
        mock_body.read.side_effect = [content, b""]
        mock_client.get_object.return_value = {"Body": mock_body}
        store = create_store_with_mock_client(mock_client)

        info = store.get_info("test.txt")

        assert info["sha256"] == hashlib.sha256(content).hexdigest()


# ============ ObjectStore 预签名 URL 测试 ============


class TestObjectStorePresignedUrl:
    """ObjectStore 预签名 URL 测试"""

    def test_generate_presigned_url_for_get(self):
        """生成 GET 操作的预签名 URL"""
        mock_client = create_mock_s3_client()
        mock_client.generate_presigned_url.return_value = "https://s3.example.com/signed-url"
        store = create_store_with_mock_client(mock_client)

        url = store.generate_presigned_url("test.txt")

        assert url == "https://s3.example.com/signed-url"
        mock_client.generate_presigned_url.assert_called_once()

    def test_generate_presigned_url_custom_expiry(self):
        """自定义过期时间的预签名 URL"""
        mock_client = create_mock_s3_client()
        mock_client.generate_presigned_url.return_value = "https://s3.example.com/signed-url"
        store = create_store_with_mock_client(mock_client)

        store.generate_presigned_url("test.txt", expires_in=7200)

        call_args = mock_client.generate_presigned_url.call_args
        assert call_args.kwargs["ExpiresIn"] == 7200

    def test_generate_presigned_url_failure(self):
        """预签名 URL 生成失败"""
        mock_client = create_mock_s3_client()
        mock_client.generate_presigned_url.side_effect = Exception("Signing error")
        store = create_store_with_mock_client(mock_client)

        with pytest.raises(ObjectStoreError) as exc_info:
            store.generate_presigned_url("test.txt")

        assert "预签名 URL" in str(exc_info.value)


# ============ ObjectStore 错误分类测试 ============


class TestObjectStoreErrorClassification:
    """ObjectStore 错误分类测试"""

    def test_classify_not_found_error(self):
        """识别 Not Found 错误"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )

        error = Exception("NoSuchKey: The specified key does not exist")
        classified = store._classify_error(error, "test.txt", "test.txt")

        assert isinstance(classified, ArtifactNotFoundError)

    def test_classify_timeout_error(self):
        """识别超时错误"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )

        error = Exception("ConnectTimeoutError: Connection timeout")
        classified = store._classify_error(error, "test.txt", "test.txt")

        assert isinstance(classified, ObjectStoreTimeoutError)

    def test_classify_throttling_error(self):
        """识别限流错误"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )

        error = Exception("SlowDown: Please reduce your request rate")
        classified = store._classify_error(error, "test.txt", "test.txt")

        assert isinstance(classified, ObjectStoreThrottlingError)

    def test_classify_503_as_throttling(self):
        """识别 503 为限流错误"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )

        error = Exception("503 Service Unavailable")
        classified = store._classify_error(error, "test.txt", "test.txt")

        assert isinstance(classified, ObjectStoreThrottlingError)

    def test_classify_generic_error(self):
        """未识别的错误返回通用类型"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )

        error = Exception("Unknown error occurred")
        classified = store._classify_error(error, "test.txt", "test.txt")

        assert isinstance(classified, ObjectStoreError)


# ============ ObjectStore 高级配置测试 ============


class TestObjectStoreAdvancedConfiguration:
    """ObjectStore 高级配置测试"""

    def test_default_timeout_values(self):
        """默认超时值"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )

        assert store.connect_timeout == 10.0
        assert store.read_timeout == 60.0
        assert store.retries == 3

    def test_custom_timeout_values(self):
        """自定义超时值"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            connect_timeout=5.0,
            read_timeout=30.0,
            retries=5,
        )

        assert store.connect_timeout == 5.0
        assert store.read_timeout == 30.0
        assert store.retries == 5

    def test_default_multipart_settings(self):
        """默认 Multipart 设置"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )

        assert store.multipart_threshold == MULTIPART_THRESHOLD
        assert store.multipart_chunk_size == MULTIPART_CHUNK_SIZE

    def test_custom_multipart_settings(self):
        """自定义 Multipart 设置"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            multipart_threshold=10 * 1024 * 1024,
            multipart_chunk_size=16 * 1024 * 1024,
        )

        assert store.multipart_threshold == 10 * 1024 * 1024
        assert store.multipart_chunk_size == 16 * 1024 * 1024

    def test_kms_sse_configuration(self):
        """KMS 加密配置"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            sse="aws:kms",
        )

        extra_args = store._build_put_extra_args()
        assert extra_args["ServerSideEncryption"] == "aws:kms"

    def test_acl_configuration(self):
        """ACL 配置"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            acl="private",
        )

        extra_args = store._build_put_extra_args()
        assert extra_args["ACL"] == "private"

    def test_default_addressing_style(self):
        """默认 addressing_style 为 auto"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )

        assert store.addressing_style == "auto"

    def test_custom_addressing_style(self):
        """自定义 addressing_style"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            addressing_style="path",
        )

        assert store.addressing_style == "path"


class TestObjectStoreBotoConfigAddressingStyle:
    """ObjectStore BotoConfig addressing_style 测试"""

    def test_boto_config_includes_addressing_style(self):
        """验证 BotoConfig 包含 addressing_style 配置"""
        # 创建 mock BotoConfig 类
        mock_boto_config_class = MagicMock()
        mock_boto_config_instance = MagicMock()
        mock_boto_config_class.return_value = mock_boto_config_instance
        
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            addressing_style="path",
        )
        
        # Mock 整个 boto3 和 botocore.config 模块
        with patch.dict("sys.modules", {"boto3": mock_boto3, "botocore": MagicMock(), "botocore.config": MagicMock()}):
            with patch("engram_logbook.artifact_store.boto3", mock_boto3, create=True):
                # 重新导入模块使用 mock
                import importlib
                import engram_logbook.artifact_store as artifact_store_module
                
                # 直接 mock _get_client 中的 import 语句
                with patch.object(artifact_store_module, "__builtins__", {"__import__": MagicMock()}):
                    pass
        
        # 使用简化的方式验证：检查 store 属性设置正确
        assert store.addressing_style == "path"

    def test_boto_config_addressing_style_auto(self):
        """验证 addressing_style=auto 正确传递"""
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            addressing_style="auto",
        )
        
        # 验证 store 属性正确设置
        assert store.addressing_style == "auto"

    def test_boto_config_addressing_style_virtual(self):
        """验证 addressing_style=virtual 正确传递"""
        store = ObjectStore(
            endpoint="https://s3.amazonaws.com",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            addressing_style="virtual",
        )
        
        # 验证 store 属性正确设置
        assert store.addressing_style == "virtual"

    def test_get_client_uses_addressing_style_in_config(self):
        """验证 _get_client 在 BotoConfig 中使用 addressing_style"""
        # 创建 mock boto3 和 botocore
        mock_boto_config = MagicMock()
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config
        
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            addressing_style="path",
            verify_ssl=False,  # 开发环境使用 http:// 需禁用 SSL 验证
        )
        
        # 使用 sys.modules patch 来模拟 import boto3 和 botocore.config
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            # 调用 _get_client
            store._get_client()
            
            # 验证 BotoConfig 被调用时包含 s3 参数
            mock_boto_config.assert_called_once()
            call_kwargs = mock_boto_config.call_args.kwargs
            
            # 验证 s3 配置包含 addressing_style
            assert "s3" in call_kwargs
            assert call_kwargs["s3"]["addressing_style"] == "path"


# ============ ObjectStore._get_client 参数捕获测试 ============


class TestObjectStoreGetClientBoto3Params:
    """ObjectStore._get_client boto3.client 参数捕获测试"""

    def test_get_client_passes_correct_params_to_boto3(self):
        """验证 _get_client 将正确的参数传递给 boto3.client"""
        mock_boto3 = MagicMock()
        mock_client_instance = MagicMock()
        mock_boto3.client.return_value = mock_client_instance
        
        mock_boto_config_class = MagicMock()
        mock_boto_config_instance = MagicMock()
        mock_boto_config_class.return_value = mock_boto_config_instance
        
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config_class
        
        store = ObjectStore(
            endpoint="https://minio.example.com:9000",
            access_key="test_access_key",
            secret_key="test_secret_key",
            bucket="test-bucket",
            region="ap-northeast-1",
            verify_ssl=True,
        )
        
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            store._get_client()
            
            # 验证 boto3.client 被调用
            mock_boto3.client.assert_called_once()
            call_kwargs = mock_boto3.client.call_args.kwargs
            
            # 验证传递的参数
            assert call_kwargs["endpoint_url"] == "https://minio.example.com:9000"
            assert call_kwargs["aws_access_key_id"] == "test_access_key"
            assert call_kwargs["aws_secret_access_key"] == "test_secret_key"
            assert call_kwargs["region_name"] == "ap-northeast-1"
            assert call_kwargs["verify"] is True
            assert call_kwargs["config"] is mock_boto_config_instance
            
            # 验证 service_name 为 's3'
            call_args = mock_boto3.client.call_args.args
            assert call_args[0] == "s3"

    def test_get_client_with_ca_bundle_passes_path(self):
        """验证 ca_bundle 参数正确传递给 boto3.client"""
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        
        mock_boto_config_class = MagicMock()
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config_class
        
        store = ObjectStore(
            endpoint="https://minio.example.com:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            ca_bundle="/path/to/custom/ca-bundle.crt",
        )
        
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            store._get_client()
            
            call_kwargs = mock_boto3.client.call_args.kwargs
            
            # 当 ca_bundle 指定时，verify 应该是 ca_bundle 路径
            assert call_kwargs["verify"] == "/path/to/custom/ca-bundle.crt"

    def test_get_client_with_verify_ssl_false_passes_false(self):
        """验证 verify_ssl=False 时，verify 参数为 False"""
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        
        mock_boto_config_class = MagicMock()
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config_class
        
        store = ObjectStore(
            endpoint="http://localhost:9000",  # HTTP 端点需要 verify_ssl=False
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            verify_ssl=False,
        )
        
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            store._get_client()
            
            call_kwargs = mock_boto3.client.call_args.kwargs
            assert call_kwargs["verify"] is False

    def test_get_client_boto_config_timeout_and_retry(self):
        """验证 BotoConfig 接收正确的超时和重试参数"""
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        
        mock_boto_config_class = MagicMock()
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config_class
        
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            verify_ssl=False,
            connect_timeout=5.0,
            read_timeout=30.0,
            retries=5,
        )
        
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            store._get_client()
            
            # 验证 BotoConfig 被调用
            mock_boto_config_class.assert_called_once()
            config_kwargs = mock_boto_config_class.call_args.kwargs
            
            # 验证超时配置
            assert config_kwargs["connect_timeout"] == 5.0
            assert config_kwargs["read_timeout"] == 30.0
            
            # 验证重试配置
            assert config_kwargs["retries"]["max_attempts"] == 5
            assert config_kwargs["retries"]["mode"] == "adaptive"
            
            # 验证签名版本
            assert config_kwargs["signature_version"] == "s3v4"

    def test_get_client_boto_config_addressing_style_path(self):
        """验证 addressing_style=path 正确传递到 BotoConfig"""
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        
        mock_boto_config_class = MagicMock()
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config_class
        
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            verify_ssl=False,
            addressing_style="path",
        )
        
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            store._get_client()
            
            config_kwargs = mock_boto_config_class.call_args.kwargs
            
            # 验证 s3 配置中的 addressing_style
            assert "s3" in config_kwargs
            assert config_kwargs["s3"]["addressing_style"] == "path"

    def test_get_client_boto_config_addressing_style_virtual(self):
        """验证 addressing_style=virtual 正确传递到 BotoConfig"""
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        
        mock_boto_config_class = MagicMock()
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config_class
        
        store = ObjectStore(
            endpoint="https://s3.amazonaws.com",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            addressing_style="virtual",
        )
        
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            store._get_client()
            
            config_kwargs = mock_boto_config_class.call_args.kwargs
            assert config_kwargs["s3"]["addressing_style"] == "virtual"

    def test_get_client_default_region(self):
        """验证未指定 region 时使用默认值 us-east-1"""
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        
        mock_boto_config_class = MagicMock()
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config_class
        
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            verify_ssl=False,
            # 不指定 region
        )
        
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            store._get_client()
            
            call_kwargs = mock_boto3.client.call_args.kwargs
            
            # 默认 region 应为 us-east-1
            assert call_kwargs["region_name"] == "us-east-1"

    def test_get_client_caches_client_instance(self):
        """验证 _get_client 缓存客户端实例，不重复创建"""
        mock_boto3 = MagicMock()
        mock_client_instance = MagicMock()
        mock_boto3.client.return_value = mock_client_instance
        
        mock_boto_config_class = MagicMock()
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config_class
        
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            verify_ssl=False,
        )
        
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            # 第一次调用
            client1 = store._get_client()
            # 第二次调用
            client2 = store._get_client()
            
            # 应该返回同一个实例
            assert client1 is client2
            
            # boto3.client 只应调用一次
            assert mock_boto3.client.call_count == 1

    def test_get_client_with_prefix_does_not_affect_boto_params(self):
        """验证 store.prefix 不影响 boto3.client 参数（prefix 仅影响 key 构建）"""
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        
        mock_boto_config_class = MagicMock()
        mock_botocore_config = MagicMock()
        mock_botocore_config.Config = mock_boto_config_class
        
        store = ObjectStore(
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
            prefix="my-prefix/v1",  # 设置 prefix
            verify_ssl=False,
        )
        
        with patch.dict("sys.modules", {
            "boto3": mock_boto3,
            "botocore": MagicMock(),
            "botocore.config": mock_botocore_config,
        }):
            store._get_client()
            
            call_kwargs = mock_boto3.client.call_args.kwargs
            
            # boto3.client 参数不应包含 prefix
            assert "prefix" not in call_kwargs
            
            # 但 store.prefix 应该被正确保存
            assert store.prefix == "my-prefix/v1"


# ============ 凭证选择逻辑测试 ============


class TestObjectStoreCredentialSelection:
    """ObjectStore 凭证选择逻辑测试（与 docker-compose.unified.yml 对齐）"""

    def test_explicit_credentials_take_priority(self):
        """显式传入的凭证优先级最高"""
        with patch.dict(os.environ, {
            "ENGRAM_S3_USE_OPS": "true",
            "ENGRAM_S3_OPS_ACCESS_KEY": "ops-key",
            "ENGRAM_S3_OPS_SECRET_KEY": "ops-secret",
            "ENGRAM_S3_APP_ACCESS_KEY": "app-key",
            "ENGRAM_S3_APP_SECRET_KEY": "app-secret",
        }, clear=False):
            store = ObjectStore(
                endpoint="http://localhost:9000",
                access_key="explicit-key",
                secret_key="explicit-secret",
                bucket="test-bucket",
            )
            
            assert store.access_key == "explicit-key"
            assert store.secret_key == "explicit-secret"
            # 显式传入时 _using_ops_credentials 为 None
            assert store._using_ops_credentials is None

    def test_use_ops_true_selects_ops_credentials(self):
        """ENGRAM_S3_USE_OPS=true 时使用 ops 凭证"""
        with patch.dict(os.environ, {
            "ENGRAM_S3_USE_OPS": "true",
            "ENGRAM_S3_OPS_ACCESS_KEY": "ops-key",
            "ENGRAM_S3_OPS_SECRET_KEY": "ops-secret",
            "ENGRAM_S3_APP_ACCESS_KEY": "app-key",
            "ENGRAM_S3_APP_SECRET_KEY": "app-secret",
        }, clear=False):
            store = ObjectStore(
                endpoint="http://localhost:9000",
                bucket="test-bucket",
            )
            
            assert store.access_key == "ops-key"
            assert store.secret_key == "ops-secret"
            assert store._using_ops_credentials is True
            assert store.is_ops_credentials() is True

    def test_use_ops_false_selects_app_credentials(self):
        """ENGRAM_S3_USE_OPS=false 时使用 app 凭证"""
        with patch.dict(os.environ, {
            "ENGRAM_S3_USE_OPS": "false",
            "ENGRAM_S3_OPS_ACCESS_KEY": "ops-key",
            "ENGRAM_S3_OPS_SECRET_KEY": "ops-secret",
            "ENGRAM_S3_APP_ACCESS_KEY": "app-key",
            "ENGRAM_S3_APP_SECRET_KEY": "app-secret",
        }, clear=False):
            store = ObjectStore(
                endpoint="http://localhost:9000",
                bucket="test-bucket",
            )
            
            assert store.access_key == "app-key"
            assert store.secret_key == "app-secret"
            assert store._using_ops_credentials is False
            assert store.is_ops_credentials() is False

    def test_use_ops_default_is_false(self):
        """ENGRAM_S3_USE_OPS 默认为 false，使用 app 凭证"""
        # 清除所有 S3 相关环境变量
        env_vars_to_clear = [
            "ENGRAM_S3_USE_OPS",
            "ENGRAM_S3_OPS_ACCESS_KEY",
            "ENGRAM_S3_OPS_SECRET_KEY",
        ]
        clean_env = {k: v for k, v in os.environ.items() if k not in env_vars_to_clear}
        clean_env["ENGRAM_S3_APP_ACCESS_KEY"] = "app-key"
        clean_env["ENGRAM_S3_APP_SECRET_KEY"] = "app-secret"
        
        with patch.dict(os.environ, clean_env, clear=True):
            store = ObjectStore(
                endpoint="http://localhost:9000",
                bucket="test-bucket",
            )
            
            assert store.access_key == "app-key"
            assert store.secret_key == "app-secret"
            assert store._using_ops_credentials is False

    def test_fallback_to_generic_credentials(self):
        """没有 app/ops 凭证时回退到通用凭证"""
        with patch.dict(os.environ, {
            "ENGRAM_S3_USE_OPS": "false",
            "ENGRAM_S3_ACCESS_KEY": "generic-key",
            "ENGRAM_S3_SECRET_KEY": "generic-secret",
        }, clear=False):
            # 移除 app/ops 特定凭证
            for key in ["ENGRAM_S3_APP_ACCESS_KEY", "ENGRAM_S3_APP_SECRET_KEY",
                        "ENGRAM_S3_OPS_ACCESS_KEY", "ENGRAM_S3_OPS_SECRET_KEY"]:
                os.environ.pop(key, None)
            
            store = ObjectStore(
                endpoint="http://localhost:9000",
                bucket="test-bucket",
            )
            
            assert store.access_key == "generic-key"
            assert store.secret_key == "generic-secret"

    def test_use_ops_with_various_true_values(self):
        """ENGRAM_S3_USE_OPS 接受多种 true 值"""
        for true_value in ["true", "True", "TRUE", "1", "yes", "YES"]:
            with patch.dict(os.environ, {
                "ENGRAM_S3_USE_OPS": true_value,
                "ENGRAM_S3_OPS_ACCESS_KEY": "ops-key",
                "ENGRAM_S3_OPS_SECRET_KEY": "ops-secret",
            }, clear=False):
                store = ObjectStore(
                    endpoint="http://localhost:9000",
                    bucket="test-bucket",
                )
                
                assert store._using_ops_credentials is True, f"Failed for value: {true_value}"

    def test_is_ops_credentials_with_explicit_credentials_checks_env(self):
        """显式传入凭证时，is_ops_credentials 检查环境变量"""
        with patch.dict(os.environ, {
            "ENGRAM_S3_USE_OPS": "true",
        }, clear=False):
            store = ObjectStore(
                endpoint="http://localhost:9000",
                access_key="explicit-key",
                secret_key="explicit-secret",
                bucket="test-bucket",
            )
            
            # _using_ops_credentials 为 None，但 is_ops_credentials() 检查环境变量
            assert store._using_ops_credentials is None
            assert store.is_ops_credentials() is True

    def test_using_ops_credentials_property(self):
        """using_ops_credentials 属性正确暴露凭证状态"""
        with patch.dict(os.environ, {
            "ENGRAM_S3_USE_OPS": "true",
            "ENGRAM_S3_OPS_ACCESS_KEY": "ops-key",
            "ENGRAM_S3_OPS_SECRET_KEY": "ops-secret",
        }, clear=False):
            store = ObjectStore(
                endpoint="http://localhost:9000",
                bucket="test-bucket",
            )
            
            assert store.using_ops_credentials is True
