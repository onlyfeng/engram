# -*- coding: utf-8 -*-
"""
test_object_store_minio_integration.py - ObjectStore MinIO 集成测试

通过环境变量 ENGRAM_MINIO_INTEGRATION=1 启用测试。

测试覆盖:
1. 小对象 put/get
2. exists 检查
3. Multipart 上传（>5MB）
4. 错误分类：访问不存在 key、错误凭证

启动 MinIO:
    docker-compose -f docker-compose.minio.yml up -d

环境变量配置:
    export ENGRAM_MINIO_INTEGRATION=1
    export ENGRAM_S3_ENDPOINT=http://localhost:9000
    export ENGRAM_S3_ACCESS_KEY=minioadmin
    export ENGRAM_S3_SECRET_KEY=minioadmin
    export ENGRAM_S3_BUCKET=engram-test

运行测试:
    pytest tests/test_object_store_minio_integration.py -v
"""

import hashlib
import os
import secrets
import sys
import time
from typing import Generator

import pytest

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.logbook.artifact_store import (
    ObjectStore,
    ObjectStoreNotConfiguredError,
    ObjectStoreConnectionError,
    ObjectStoreUploadError,
    ObjectStoreDownloadError,
    ArtifactNotFoundError,
    MULTIPART_THRESHOLD,
)


# ============ 测试启用条件 ============

MINIO_INTEGRATION_ENABLED = os.environ.get("ENGRAM_MINIO_INTEGRATION", "").lower() in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(
    not MINIO_INTEGRATION_ENABLED,
    reason="MinIO 集成测试未启用，设置 ENGRAM_MINIO_INTEGRATION=1 启用"
)


# ============ Fixtures ============


@pytest.fixture(scope="module")
def minio_config():
    """MinIO 配置（从环境变量读取）"""
    config = {
        "endpoint": os.environ.get("ENGRAM_S3_ENDPOINT", "http://localhost:9000"),
        "access_key": os.environ.get("ENGRAM_S3_ACCESS_KEY", "minioadmin"),
        "secret_key": os.environ.get("ENGRAM_S3_SECRET_KEY", "minioadmin"),
        "bucket": os.environ.get("ENGRAM_S3_BUCKET", "engram-test"),
        "region": os.environ.get("ENGRAM_S3_REGION", "us-east-1"),
    }
    return config


@pytest.fixture(scope="module")
def object_store(minio_config):
    """创建 ObjectStore 实例"""
    store = ObjectStore(
        endpoint=minio_config["endpoint"],
        access_key=minio_config["access_key"],
        secret_key=minio_config["secret_key"],
        bucket=minio_config["bucket"],
        region=minio_config["region"],
    )
    return store


@pytest.fixture
def unique_key():
    """生成唯一的对象键（避免测试间冲突）"""
    timestamp = int(time.time() * 1000)
    random_suffix = secrets.token_hex(4)
    return f"test/{timestamp}_{random_suffix}"


@pytest.fixture
def cleanup_keys(object_store):
    """
    收集测试创建的对象键，测试结束后清理
    
    用法:
        def test_xxx(cleanup_keys):
            key = "test/my_object.txt"
            cleanup_keys.append(key)
            store.put(key, b"content")
    """
    keys = []
    yield keys
    
    # 清理测试创建的对象
    client = object_store._get_client()
    for key in keys:
        try:
            full_key = object_store._object_key(key)
            client.delete_object(Bucket=object_store.bucket, Key=full_key)
        except Exception:
            pass  # 忽略清理失败


# ============ 连接测试 ============


class TestMinIOConnection:
    """MinIO 连接测试"""

    def test_connection_success(self, object_store):
        """成功连接到 MinIO"""
        # _get_client 应成功初始化客户端
        client = object_store._get_client()
        assert client is not None
        
        # 尝试列出 bucket 验证连接
        response = client.list_buckets()
        bucket_names = [b["Name"] for b in response.get("Buckets", [])]
        assert object_store.bucket in bucket_names, (
            f"Bucket {object_store.bucket} 不存在，可用 buckets: {bucket_names}"
        )

    def test_wrong_credentials_error(self, minio_config):
        """错误凭证应导致操作失败"""
        store = ObjectStore(
            endpoint=minio_config["endpoint"],
            access_key="wrong_key",
            secret_key="wrong_secret",
            bucket=minio_config["bucket"],
        )
        
        # 连接时不会报错，但操作时会失败
        with pytest.raises((ObjectStoreUploadError, ObjectStoreConnectionError, Exception)) as exc_info:
            store.put("test/wrong_creds.txt", b"content")
        
        # 验证错误信息中包含认证相关信息
        error_str = str(exc_info.value).lower()
        # MinIO 可能返回不同的错误消息
        assert any(kw in error_str for kw in [
            "access", "denied", "credential", "signature", "forbidden",
            "invalidaccesskey", "上传制品失败"
        ])


# ============ 小对象操作测试 ============


class TestSmallObjectOperations:
    """小对象 put/get/exists 测试"""

    def test_put_get_bytes(self, object_store, unique_key, cleanup_keys):
        """put 和 get 字节内容"""
        key = f"{unique_key}/bytes.txt"
        cleanup_keys.append(key)
        
        content = b"Hello, MinIO! " + secrets.token_bytes(32)
        expected_sha256 = hashlib.sha256(content).hexdigest()
        
        # Put
        result = object_store.put(key, content)
        
        assert result["uri"] == key
        assert result["sha256"] == expected_sha256
        assert result["size_bytes"] == len(content)
        
        # Get
        retrieved = object_store.get(key)
        assert retrieved == content

    def test_put_get_string(self, object_store, unique_key, cleanup_keys):
        """put 和 get 字符串内容"""
        key = f"{unique_key}/string.txt"
        cleanup_keys.append(key)
        
        content_str = "你好，MinIO！这是 UTF-8 字符串测试。🚀"
        content_bytes = content_str.encode("utf-8")
        expected_sha256 = hashlib.sha256(content_bytes).hexdigest()
        
        # Put string
        result = object_store.put(key, content_str)
        
        assert result["sha256"] == expected_sha256
        assert result["size_bytes"] == len(content_bytes)
        
        # Get returns bytes
        retrieved = object_store.get(key)
        assert retrieved == content_bytes
        assert retrieved.decode("utf-8") == content_str

    def test_put_get_iterator(self, object_store, unique_key, cleanup_keys):
        """put 迭代器内容"""
        key = f"{unique_key}/iterator.txt"
        cleanup_keys.append(key)
        
        chunks = [b"chunk1_", b"chunk2_", b"chunk3_end"]
        full_content = b"".join(chunks)
        expected_sha256 = hashlib.sha256(full_content).hexdigest()
        
        # Put iterator
        result = object_store.put(key, iter(chunks))
        
        assert result["sha256"] == expected_sha256
        assert result["size_bytes"] == len(full_content)
        
        # Get
        retrieved = object_store.get(key)
        assert retrieved == full_content

    def test_exists_true(self, object_store, unique_key, cleanup_keys):
        """exists 对存在的对象返回 True"""
        key = f"{unique_key}/exists_true.txt"
        cleanup_keys.append(key)
        
        # 先创建对象
        object_store.put(key, b"content for exists test")
        
        # 检查 exists
        assert object_store.exists(key) is True

    def test_exists_false(self, object_store, unique_key):
        """exists 对不存在的对象返回 False"""
        key = f"{unique_key}/definitely_not_exists.txt"
        
        assert object_store.exists(key) is False

    def test_overwrite_object(self, object_store, unique_key, cleanup_keys):
        """覆盖已存在的对象"""
        key = f"{unique_key}/overwrite.txt"
        cleanup_keys.append(key)
        
        content_v1 = b"version 1"
        content_v2 = b"version 2 - updated content"
        
        # 写入 v1
        result1 = object_store.put(key, content_v1)
        assert result1["size_bytes"] == len(content_v1)
        
        # 覆盖为 v2
        result2 = object_store.put(key, content_v2)
        assert result2["size_bytes"] == len(content_v2)
        
        # 读取应为 v2
        retrieved = object_store.get(key)
        assert retrieved == content_v2


# ============ Multipart 上传测试 ============


class TestMultipartUpload:
    """Multipart 上传测试（>5MB）"""

    def test_multipart_upload_6mb(self, object_store, unique_key, cleanup_keys):
        """6MB 文件触发 Multipart 上传"""
        key = f"{unique_key}/multipart_6mb.bin"
        cleanup_keys.append(key)
        
        # 创建 6MB 内容（超过 5MB 阈值）
        size = 6 * 1024 * 1024
        content = secrets.token_bytes(size)
        expected_sha256 = hashlib.sha256(content).hexdigest()
        
        # Put - 应使用 multipart
        result = object_store.put(key, content)
        
        assert result["uri"] == key
        assert result["sha256"] == expected_sha256
        assert result["size_bytes"] == size
        
        # Get 并验证完整性
        retrieved = object_store.get(key)
        assert len(retrieved) == size
        assert hashlib.sha256(retrieved).hexdigest() == expected_sha256

    def test_multipart_upload_iterator(self, object_store, unique_key, cleanup_keys):
        """迭代器大内容触发 Multipart 上传"""
        key = f"{unique_key}/multipart_iter.bin"
        cleanup_keys.append(key)
        
        # 创建多个 chunks，总大小超过阈值
        chunk_size = 2 * 1024 * 1024  # 2MB per chunk
        num_chunks = 4  # 总计 8MB
        
        # 预生成 chunks 以便计算 sha256
        chunks = [secrets.token_bytes(chunk_size) for _ in range(num_chunks)]
        full_content = b"".join(chunks)
        expected_sha256 = hashlib.sha256(full_content).hexdigest()
        
        # Put iterator
        result = object_store.put(key, iter(chunks))
        
        assert result["sha256"] == expected_sha256
        assert result["size_bytes"] == len(full_content)
        
        # 验证
        retrieved = object_store.get(key)
        assert hashlib.sha256(retrieved).hexdigest() == expected_sha256

    def test_multipart_threshold_boundary(self, object_store, unique_key, cleanup_keys):
        """刚好达到 Multipart 阈值边界"""
        key = f"{unique_key}/boundary.bin"
        cleanup_keys.append(key)
        
        # 刚好 5MB - 应该不触发 multipart（阈值是 >=）
        size = MULTIPART_THRESHOLD
        content = secrets.token_bytes(size)
        expected_sha256 = hashlib.sha256(content).hexdigest()
        
        result = object_store.put(key, content)
        
        assert result["size_bytes"] == size
        
        retrieved = object_store.get(key)
        assert hashlib.sha256(retrieved).hexdigest() == expected_sha256


# ============ 错误处理测试 ============


class TestErrorHandling:
    """错误分类和异常处理测试"""

    def test_get_nonexistent_key(self, object_store, unique_key):
        """访问不存在的 key 应抛出 ArtifactNotFoundError"""
        key = f"{unique_key}/nonexistent_object.txt"
        
        with pytest.raises(ArtifactNotFoundError) as exc_info:
            object_store.get(key)
        
        error = exc_info.value
        assert "不存在" in str(error) or "NoSuchKey" in str(error) or "not found" in str(error).lower()

    def test_get_info_nonexistent_key(self, object_store, unique_key):
        """get_info 对不存在的 key 应抛出 ArtifactNotFoundError"""
        key = f"{unique_key}/nonexistent_for_info.txt"
        
        with pytest.raises(ArtifactNotFoundError):
            object_store.get_info(key)

    def test_wrong_bucket_error(self, minio_config):
        """访问不存在的 bucket 应报错"""
        store = ObjectStore(
            endpoint=minio_config["endpoint"],
            access_key=minio_config["access_key"],
            secret_key=minio_config["secret_key"],
            bucket="definitely-nonexistent-bucket-12345",
        )
        
        with pytest.raises((ObjectStoreUploadError, ObjectStoreDownloadError, Exception)):
            store.put("test.txt", b"content")


# ============ 元数据和 URL 测试 ============


class TestMetadataAndUrl:
    """元数据和 URL 相关测试"""

    def test_get_info_returns_metadata(self, object_store, unique_key, cleanup_keys):
        """get_info 返回正确的元数据"""
        key = f"{unique_key}/metadata.txt"
        cleanup_keys.append(key)
        
        content = b"content for metadata test"
        expected_sha256 = hashlib.sha256(content).hexdigest()
        
        object_store.put(key, content)
        
        info = object_store.get_info(key)
        
        assert info["uri"] == key
        assert info["sha256"] == expected_sha256
        assert info["size_bytes"] == len(content)

    def test_resolve_returns_s3_url(self, object_store, unique_key):
        """resolve 返回 S3 URL 格式"""
        key = f"{unique_key}/resolve.txt"
        
        url = object_store.resolve(key)
        
        assert url.startswith("s3://")
        assert object_store.bucket in url
        assert key in url

    def test_presigned_url_generation(self, object_store, unique_key, cleanup_keys):
        """生成预签名 URL"""
        key = f"{unique_key}/presigned.txt"
        cleanup_keys.append(key)
        
        content = b"content for presigned url"
        object_store.put(key, content)
        
        # 生成预签名 URL
        presigned_url = object_store.generate_presigned_url(key, expires_in=3600)
        
        assert presigned_url is not None
        assert "http" in presigned_url
        # URL 应包含签名参数
        assert "Signature" in presigned_url or "X-Amz-Signature" in presigned_url


# ============ 流式下载测试 ============


class TestStreamDownload:
    """流式下载测试"""

    def test_get_stream_small_file(self, object_store, unique_key, cleanup_keys):
        """流式下载小文件"""
        key = f"{unique_key}/stream_small.txt"
        cleanup_keys.append(key)
        
        content = b"content for stream test " * 100
        object_store.put(key, content)
        
        # 流式读取
        chunks = list(object_store.get_stream(key))
        retrieved = b"".join(chunks)
        
        assert retrieved == content

    def test_get_stream_large_file(self, object_store, unique_key, cleanup_keys):
        """流式下载大文件"""
        key = f"{unique_key}/stream_large.bin"
        cleanup_keys.append(key)
        
        # 3MB 文件
        size = 3 * 1024 * 1024
        content = secrets.token_bytes(size)
        expected_sha256 = hashlib.sha256(content).hexdigest()
        
        object_store.put(key, content)
        
        # 流式读取并计算 sha256
        hasher = hashlib.sha256()
        total_size = 0
        for chunk in object_store.get_stream(key, chunk_size=65536):
            hasher.update(chunk)
            total_size += len(chunk)
        
        assert total_size == size
        assert hasher.hexdigest() == expected_sha256


# ============ 前缀测试 ============


class TestPrefixOperations:
    """带前缀的操作测试"""

    def test_operations_with_prefix(self, minio_config, unique_key, cleanup_keys):
        """带前缀的 put/get/exists"""
        prefix = "test_prefix/v1"
        store = ObjectStore(
            endpoint=minio_config["endpoint"],
            access_key=minio_config["access_key"],
            secret_key=minio_config["secret_key"],
            bucket=minio_config["bucket"],
            prefix=prefix,
        )
        
        key = f"{unique_key}/prefixed.txt"
        cleanup_keys.append(f"{prefix}/{key}")  # 实际 key 包含 prefix
        
        content = b"content with prefix"
        
        # Put
        result = store.put(key, content)
        assert result["uri"] == key
        
        # Exists
        assert store.exists(key) is True
        
        # Get
        retrieved = store.get(key)
        assert retrieved == content
        
        # Resolve
        url = store.resolve(key)
        assert prefix in url
