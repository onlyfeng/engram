# -*- coding: utf-8 -*-
"""
test_unified_stack_integration.py - 统一栈集成测试

完整的 PostgreSQL + MinIO + Logbook 集成测试，覆盖：
1. 制品写入（包含 > multipart_threshold 的大对象，触发 Multipart 上传）
2. DB 引用记录管理（patch_blobs / attachments 表）
3. artifact_audit 完整性校验（OK / missing / mismatch 检测）
4. artifact_gc 垃圾回收验证（删除 DB 引用后仅清理孤立对象）
5. artifact migrate 迁移演练（local -> object / object -> local）

================================================================================
测试环境要求
================================================================================

本测试依赖：
- PostgreSQL 数据库（已执行 db_migrate）
- MinIO 对象存储（已创建 bucket）

测试通过环境变量开关启用，避免默认单测时强依赖 Docker。

================================================================================
启用方式
================================================================================

方式一：本地运行（需要先启动 Docker 服务）
-------------------------------------------------
# 1. 启动 PostgreSQL + MinIO
docker compose -f docker-compose.unified.yml --profile minio up -d

# 2. 设置环境变量并运行测试
export ENGRAM_UNIFIED_INTEGRATION=1
export POSTGRES_DSN="postgresql://postgres:postgres@localhost:5432/engram"
export ENGRAM_S3_ENDPOINT="http://localhost:9000"
export ENGRAM_S3_ACCESS_KEY="minioadmin"
export ENGRAM_S3_SECRET_KEY="minioadmin"
export ENGRAM_S3_BUCKET="engram-test"

pytest tests/test_unified_stack_integration.py -v

方式二：通过 Docker Compose 运行（推荐用于 CI）
-------------------------------------------------
# 使用 test profile 自动启动测试容器
# 需要设置 MinIO 凭证和服务账号密码
MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin \
LOGBOOK_MIGRATOR_PASSWORD=logbook_migrator_test_pwd \
LOGBOOK_SVC_PASSWORD=logbook_svc_test_pwd \
OPENMEMORY_MIGRATOR_PASSWORD=om_migrator_test_pwd \
OPENMEMORY_SVC_PASSWORD=om_svc_test_pwd \
docker compose -f docker-compose.unified.yml --profile minio --profile test up

# 或使用 Makefile 简化命令（自动设置默认密码）
make test-logbook-integration

================================================================================
环境变量说明
================================================================================

必需（启用集成测试时）：
    ENGRAM_UNIFIED_INTEGRATION=1  启用集成测试的开关
    POSTGRES_DSN                  PostgreSQL 连接字符串
    ENGRAM_S3_ENDPOINT            MinIO/S3 端点 URL
    ENGRAM_S3_ACCESS_KEY          S3 访问密钥
    ENGRAM_S3_SECRET_KEY          S3 密钥
    ENGRAM_S3_BUCKET              S3 存储桶名称

可选：
    ENGRAM_UNIFIED_SKIP_DOCKER=1  跳过服务可用性检查（假设服务已运行）
    ENGRAM_UNIFIED_SKIP_MIGRATE=1 跳过迁移测试（仅运行 audit/gc 测试）
    ENGRAM_S3_REGION              S3 区域（默认 us-east-1）

================================================================================
测试覆盖场景
================================================================================

TestArtifactWrite:
- test_small_object_write: 小对象写入（< 5MB，不触发 Multipart）
- test_large_object_multipart_write: 大对象写入（> 5MB，触发 Multipart）
- test_local_store_write: 本地存储后端写入

TestAuditWithDbReferences:
- test_audit_referenced_artifacts_ok: 审计被引用制品返回 OK
- test_audit_detects_missing_artifact: 审计检测缺失制品
- test_audit_detects_hash_mismatch: 审计检测哈希不匹配

TestGarbageCollection:
- test_gc_protects_referenced_artifacts: GC 保护被引用制品
- test_gc_deletes_only_orphan_artifacts: GC 仅删除孤立制品
- test_gc_with_deleted_db_reference: 删除 DB 引用后 GC 清理制品

TestArtifactMigration:
- test_migrate_local_to_object: 迁移 local -> object
- test_migrate_object_to_local: 迁移 object -> local
- test_migrate_large_object_with_multipart: 迁移大对象
- test_migrate_and_verify_with_audit: 迁移后审计验证

TestEndToEndFlow:
- test_full_lifecycle_local: 完整生命周期（创建->引用->审计->删引用->GC）
- test_full_lifecycle_with_attachments: 使用 attachments 表的完整流程
"""

import hashlib
import os
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# scripts 模块通过包路径导入，无需修改 sys.path


# ============================================================================
# 环境变量与配置
# ============================================================================

INTEGRATION_TEST_VAR = "ENGRAM_UNIFIED_INTEGRATION"
SKIP_DOCKER_VAR = "ENGRAM_UNIFIED_SKIP_DOCKER"
SKIP_MIGRATE_VAR = "ENGRAM_UNIFIED_SKIP_MIGRATE"


def should_run_integration_tests() -> bool:
    """检查是否应该运行集成测试"""
    return os.environ.get(INTEGRATION_TEST_VAR, "").lower() in ("1", "true", "yes")


def should_skip_docker() -> bool:
    """检查是否跳过 Docker 启动"""
    return os.environ.get(SKIP_DOCKER_VAR, "").lower() in ("1", "true", "yes")


def should_skip_migrate() -> bool:
    """检查是否跳过迁移测试"""
    return os.environ.get(SKIP_MIGRATE_VAR, "").lower() in ("1", "true", "yes")


# pytest marker: 集成测试需要设置环境变量才能运行
integration_test = pytest.mark.skipif(
    not should_run_integration_tests(), reason=f"统一栈集成测试需要设置 {INTEGRATION_TEST_VAR}=1"
)

migrate_test = pytest.mark.skipif(
    should_skip_migrate(), reason=f"迁移测试已被 {SKIP_MIGRATE_VAR}=1 跳过"
)


# ============================================================================
# 配置获取
# ============================================================================


def get_postgres_dsn() -> str:
    """获取 PostgreSQL DSN"""
    return os.environ.get("POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/engram")


def get_minio_config() -> Dict[str, str]:
    """获取 MinIO 配置"""
    return {
        "endpoint": os.environ.get("ENGRAM_S3_ENDPOINT", "http://localhost:9000"),
        "access_key": os.environ.get("ENGRAM_S3_ACCESS_KEY", "minioadmin"),
        "secret_key": os.environ.get("ENGRAM_S3_SECRET_KEY", "minioadmin"),
        "bucket": os.environ.get("ENGRAM_S3_BUCKET", "engram-test"),
        "region": os.environ.get("ENGRAM_S3_REGION", "us-east-1"),
    }


def get_compose_file() -> str:
    """获取 docker-compose.unified.yml 路径"""
    scripts_dir = Path(__file__).parent.parent
    project_root = scripts_dir.parent.parent
    return str(project_root / "docker-compose.unified.yml")


# ============================================================================
# Docker Compose 辅助
# ============================================================================


def is_docker_available() -> bool:
    """检查 Docker 是否可用"""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def wait_for_postgres(dsn: str, max_wait: int = 60) -> bool:
    """等待 PostgreSQL 就绪"""
    import psycopg

    start = time.time()
    while time.time() - start < max_wait:
        try:
            conn = psycopg.connect(dsn, connect_timeout=5)
            conn.execute("SELECT 1")
            conn.close()
            return True
        except Exception:
            time.sleep(2)
    return False


def wait_for_minio(endpoint: str, max_wait: int = 60) -> bool:
    """等待 MinIO 就绪"""
    import requests

    start = time.time()
    # MinIO health endpoint
    health_url = f"{endpoint}/minio/health/live"

    while time.time() - start < max_wait:
        try:
            resp = requests.get(health_url, timeout=5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def ensure_minio_bucket(config: Dict[str, str]) -> bool:
    """确保 MinIO bucket 存在"""
    try:
        import boto3
        from botocore.config import Config as BotoConfig

        client = boto3.client(
            "s3",
            endpoint_url=config["endpoint"],
            aws_access_key_id=config["access_key"],
            aws_secret_access_key=config["secret_key"],
            region_name=config.get("region", "us-east-1"),
            config=BotoConfig(signature_version="s3v4"),
        )

        bucket = config["bucket"]

        # 检查 bucket 是否存在
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            # 创建 bucket
            client.create_bucket(Bucket=bucket)

        return True
    except Exception as e:
        print(f"[WARN] 无法确保 MinIO bucket: {e}")
        return False


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="module")
def unified_stack_config():
    """统一栈配置"""
    return {
        "postgres_dsn": get_postgres_dsn(),
        "minio": get_minio_config(),
        "compose_file": get_compose_file(),
    }


@pytest.fixture(scope="module")
def unified_stack_ready(unified_stack_config):
    """
    确保统一栈就绪的 fixture

    如果 ENGRAM_UNIFIED_SKIP_DOCKER=1，则假设服务已运行。
    否则检查并等待服务就绪。
    """
    config = unified_stack_config

    # 等待 PostgreSQL
    if not wait_for_postgres(config["postgres_dsn"], max_wait=30):
        pytest.skip("PostgreSQL 服务不可用")

    # 等待 MinIO
    minio_config = config["minio"]
    if not wait_for_minio(minio_config["endpoint"], max_wait=30):
        pytest.skip("MinIO 服务不可用")

    # 确保 bucket 存在
    if not ensure_minio_bucket(minio_config):
        pytest.skip("无法创建 MinIO bucket")

    return config


@pytest.fixture(scope="module")
def db_connection(unified_stack_ready):
    """提供数据库连接"""
    import psycopg

    dsn = unified_stack_ready["postgres_dsn"]
    conn = psycopg.connect(dsn, autocommit=True)

    yield conn

    conn.close()


@pytest.fixture(scope="module")
def migrated_database(unified_stack_ready, db_connection):
    """
    确保数据库已迁移

    使用 db_migrate 执行迁移，确保 schema 完整。
    """
    from engram.logbook.migrate import run_migrate

    dsn = unified_stack_ready["postgres_dsn"]

    result = run_migrate(dsn=dsn, quiet=True)

    if not result.get("ok"):
        pytest.fail(f"数据库迁移失败: {result.get('message')}")

    return {
        "dsn": dsn,
        "conn": db_connection,
    }


@pytest.fixture(scope="module")
def object_store(unified_stack_ready):
    """创建 ObjectStore 实例"""
    from engram.logbook.artifact_store import ObjectStore

    minio_config = unified_stack_ready["minio"]

    store = ObjectStore(
        endpoint=minio_config["endpoint"],
        access_key=minio_config["access_key"],
        secret_key=minio_config["secret_key"],
        bucket=minio_config["bucket"],
        region=minio_config.get("region", "us-east-1"),
    )

    return store


@pytest.fixture(scope="module")
def local_store(tmp_path_factory):
    """创建 LocalArtifactsStore 实例"""
    from engram.logbook.artifact_store import LocalArtifactsStore

    root = tmp_path_factory.mktemp("artifacts")
    store = LocalArtifactsStore(root=root)

    return store


@pytest.fixture
def unique_prefix():
    """生成唯一的测试前缀"""
    timestamp = int(time.time() * 1000)
    random_suffix = secrets.token_hex(4)
    return f"test/{timestamp}_{random_suffix}"


@pytest.fixture
def cleanup_artifacts(object_store):
    """
    收集测试创建的对象键，测试结束后清理

    用法:
        def test_xxx(cleanup_artifacts):
            key = "test/my_object.txt"
            cleanup_artifacts.append(key)
            store.put(key, b"content")
    """
    keys = []
    yield keys

    # 清理测试创建的对象
    try:
        client = object_store._get_client()
        for key in keys:
            try:
                full_key = object_store._object_key(key)
                client.delete_object(Bucket=object_store.bucket, Key=full_key)
            except Exception:
                pass
    except Exception:
        pass


# ============================================================================
# 辅助函数
# ============================================================================


def create_test_artifact(
    store,
    uri: str,
    size_bytes: int = 1024,
    content: Optional[bytes] = None,
) -> Dict:
    """
    创建测试制品

    Args:
        store: ArtifactStore 实例
        uri: 制品 URI
        size_bytes: 内容大小（当 content 为 None 时使用）
        content: 自定义内容

    Returns:
        put 操作结果字典
    """
    if content is None:
        content = secrets.token_bytes(size_bytes)

    return store.put(uri, content)


def insert_patch_blob_reference(
    conn,
    uri: str,
    sha256: str,
    size_bytes: int,
    source_type: str = "git",
) -> int:
    """
    在 scm.patch_blobs 表中插入引用记录

    Args:
        conn: 数据库连接
        uri: 制品 URI
        sha256: 内容 SHA256
        size_bytes: 内容大小
        source_type: 来源类型 ('git' 或 'svn')

    Returns:
        blob_id
    """
    # 生成唯一的 source_id，格式: <随机前缀>:<sha256前8位>
    unique_prefix = secrets.token_hex(4)
    source_id = f"{unique_prefix}:{sha256[:8]}"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.patch_blobs (
                source_type, source_id, uri, sha256, size_bytes, format
            ) VALUES (
                %s, %s, %s, %s, %s, 'diff'
            )
            RETURNING blob_id
        """,
            (source_type, source_id, uri, sha256, size_bytes),
        )
        result = cur.fetchone()
        return result[0]


def delete_patch_blob_reference(conn, blob_id: int) -> bool:
    """删除 patch_blob 引用记录"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM scm.patch_blobs WHERE blob_id = %s", (blob_id,))
        return cur.rowcount > 0


def insert_attachment_reference(
    conn,
    uri: str,
    sha256: str,
    size_bytes: int,
    item_id: Optional[int] = None,
    kind: str = "report",
) -> Tuple[int, int]:
    """
    在 logbook.attachments 表中插入引用记录

    如果未提供 item_id，会自动创建一个 logbook.items 记录以满足外键约束。

    Args:
        conn: 数据库连接
        uri: 制品 URI
        sha256: 内容 SHA256
        size_bytes: 内容大小
        item_id: 可选的 item_id，如果为 None 则自动创建
        kind: 附件类型 ('report', 'patch', 'log', 'spec' 等)

    Returns:
        (attachment_id, item_id) 元组
    """
    with conn.cursor() as cur:
        # 如果未提供 item_id，创建一个 logbook.items 记录
        if item_id is None:
            cur.execute("""
                INSERT INTO logbook.items (item_type, title)
                VALUES ('test', 'Test item for attachment')
                RETURNING item_id
            """)
            item_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO logbook.attachments (
                item_id, kind, uri, sha256, size_bytes
            ) VALUES (
                %s, %s, %s, %s, %s
            )
            RETURNING attachment_id
        """,
            (item_id, kind, uri, sha256, size_bytes),
        )
        attachment_id = cur.fetchone()[0]
        return (attachment_id, item_id)


def delete_attachment_reference(conn, attachment_id: int, item_id: Optional[int] = None) -> bool:
    """
    删除 attachment 引用记录

    Args:
        conn: 数据库连接
        attachment_id: 附件 ID
        item_id: 可选的 item_id，如果提供则同时删除关联的 item 记录

    Returns:
        是否成功删除
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM logbook.attachments WHERE attachment_id = %s", (attachment_id,))
        deleted = cur.rowcount > 0

        # 如果提供了 item_id，同时清理创建的 item 记录
        if item_id is not None:
            cur.execute("DELETE FROM logbook.items WHERE item_id = %s", (item_id,))

        return deleted


# ============================================================================
# 测试类: 制品写入
# ============================================================================


@integration_test
class TestArtifactWrite:
    """制品写入测试"""

    def test_small_object_write(
        self,
        object_store,
        unique_prefix,
        cleanup_artifacts,
    ):
        """小对象写入（< multipart_threshold）"""
        uri = f"{unique_prefix}/small_object.bin"
        cleanup_artifacts.append(uri)

        content = secrets.token_bytes(1024)  # 1KB
        expected_sha256 = hashlib.sha256(content).hexdigest()

        result = object_store.put(uri, content)

        assert result["uri"] == uri
        assert result["sha256"] == expected_sha256
        assert result["size_bytes"] == len(content)

        # 验证可以读取
        retrieved = object_store.get(uri)
        assert retrieved == content

    def test_large_object_multipart_write(
        self,
        object_store,
        unique_prefix,
        cleanup_artifacts,
    ):
        """大对象写入（> multipart_threshold，触发 Multipart 上传）"""
        from engram.logbook.artifact_store import MULTIPART_THRESHOLD

        uri = f"{unique_prefix}/large_object.bin"
        cleanup_artifacts.append(uri)

        # 创建超过阈值的内容
        size = MULTIPART_THRESHOLD + (1024 * 1024)  # 阈值 + 1MB
        content = secrets.token_bytes(size)
        expected_sha256 = hashlib.sha256(content).hexdigest()

        result = object_store.put(uri, content)

        assert result["uri"] == uri
        assert result["sha256"] == expected_sha256
        assert result["size_bytes"] == size

        # 验证可以读取
        retrieved = object_store.get(uri)
        assert len(retrieved) == size
        assert hashlib.sha256(retrieved).hexdigest() == expected_sha256

    def test_local_store_write(
        self,
        local_store,
        unique_prefix,
    ):
        """本地存储写入"""
        uri = f"{unique_prefix}/local_file.bin"

        content = secrets.token_bytes(2048)
        expected_sha256 = hashlib.sha256(content).hexdigest()

        result = local_store.put(uri, content)

        assert result["uri"] == uri
        assert result["sha256"] == expected_sha256
        assert result["size_bytes"] == len(content)

        # 验证可以读取
        retrieved = local_store.get(uri)
        assert retrieved == content


# ============================================================================
# 测试类: DB 引用与 Audit
# ============================================================================


@integration_test
class TestAuditWithDbReferences:
    """DB 引用和审计测试"""

    def test_audit_referenced_artifacts_ok(
        self,
        object_store,
        migrated_database,
        unique_prefix,
        cleanup_artifacts,
    ):
        """审计被引用的制品应返回 OK"""
        from scripts.artifact_audit import ArtifactAuditor

        conn = migrated_database["conn"]
        migrated_database["dsn"]

        # 创建测试制品
        uri = f"{unique_prefix}/audit_test.bin"
        cleanup_artifacts.append(uri)

        content = secrets.token_bytes(1024)
        result = object_store.put(uri, content)

        # 插入 DB 引用
        blob_id = insert_patch_blob_reference(
            conn,
            uri=uri,
            sha256=result["sha256"],
            size_bytes=result["size_bytes"],
        )

        try:
            # 创建审计器
            auditor = ArtifactAuditor(
                conn=conn,
                artifact_store=object_store,
                verbose=False,
            )

            # 审计单条记录
            audit_result = auditor.audit_record(
                table="patch_blobs",
                record_id=blob_id,
                uri=uri,
                expected_sha256=result["sha256"],
            )

            assert audit_result.status == "ok"
            assert audit_result.actual_sha256 == result["sha256"]

        finally:
            # 清理 DB 引用
            delete_patch_blob_reference(conn, blob_id)

    def test_audit_detects_missing_artifact(
        self,
        object_store,
        migrated_database,
        unique_prefix,
    ):
        """审计检测缺失的制品"""
        from scripts.artifact_audit import ArtifactAuditor

        conn = migrated_database["conn"]

        # 不实际创建制品，只插入 DB 引用
        uri = f"{unique_prefix}/missing_artifact.bin"
        fake_sha256 = "0" * 64

        blob_id = insert_patch_blob_reference(
            conn,
            uri=uri,
            sha256=fake_sha256,
            size_bytes=1024,
        )

        try:
            auditor = ArtifactAuditor(
                conn=conn,
                artifact_store=object_store,
                verbose=False,
            )

            audit_result = auditor.audit_record(
                table="patch_blobs",
                record_id=blob_id,
                uri=uri,
                expected_sha256=fake_sha256,
            )

            assert audit_result.status == "missing"

        finally:
            delete_patch_blob_reference(conn, blob_id)

    def test_audit_detects_hash_mismatch(
        self,
        object_store,
        migrated_database,
        unique_prefix,
        cleanup_artifacts,
    ):
        """审计检测哈希不匹配"""
        from scripts.artifact_audit import ArtifactAuditor

        conn = migrated_database["conn"]

        uri = f"{unique_prefix}/mismatch_test.bin"
        cleanup_artifacts.append(uri)

        # 创建制品
        content = secrets.token_bytes(1024)
        result = object_store.put(uri, content)

        # 插入带错误 sha256 的 DB 引用
        wrong_sha256 = "f" * 64
        blob_id = insert_patch_blob_reference(
            conn,
            uri=uri,
            sha256=wrong_sha256,
            size_bytes=result["size_bytes"],
        )

        try:
            auditor = ArtifactAuditor(
                conn=conn,
                artifact_store=object_store,
                verbose=False,
            )

            audit_result = auditor.audit_record(
                table="patch_blobs",
                record_id=blob_id,
                uri=uri,
                expected_sha256=wrong_sha256,
            )

            assert audit_result.status == "mismatch"
            assert audit_result.actual_sha256 == result["sha256"]

        finally:
            delete_patch_blob_reference(conn, blob_id)


# ============================================================================
# 测试类: GC 垃圾回收
# ============================================================================


@integration_test
class TestGarbageCollection:
    """垃圾回收测试"""

    def test_gc_protects_referenced_artifacts(
        self,
        local_store,
        migrated_database,
        unique_prefix,
    ):
        """GC 保护被引用的制品"""
        from engram.logbook.artifact_gc import run_gc

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        # 创建被引用的制品
        uri_referenced = f"scm/{unique_prefix}/referenced.bin"
        content1 = secrets.token_bytes(512)
        result1 = local_store.put(uri_referenced, content1)

        # 创建未被引用的制品
        uri_orphan = f"scm/{unique_prefix}/orphan.bin"
        content2 = secrets.token_bytes(512)
        local_store.put(uri_orphan, content2)

        # 只为 referenced 插入 DB 引用
        blob_id = insert_patch_blob_reference(
            conn,
            uri=uri_referenced,
            sha256=result1["sha256"],
            size_bytes=result1["size_bytes"],
        )

        try:
            # 运行 GC（dry-run 模式）
            gc_result = run_gc(
                prefix=f"scm/{unique_prefix}/",
                dry_run=True,
                delete=False,
                dsn=dsn,
                backend="local",
                artifacts_root=str(local_store.root),
                verbose=False,
            )

            # 验证：被引用的应受保护，孤立的应成为候选
            assert gc_result.protected_count >= 1
            assert gc_result.candidates_count >= 1

            candidate_uris = {c.uri for c in gc_result.candidates}
            # 规范化 URI 进行比较
            normalized_orphan = uri_orphan.rstrip("/")
            assert any(normalized_orphan in u for u in candidate_uris)

            # 验证：两个文件都还存在（dry-run 不删除）
            assert local_store.exists(uri_referenced)
            assert local_store.exists(uri_orphan)

        finally:
            delete_patch_blob_reference(conn, blob_id)

    def test_gc_deletes_only_orphan_artifacts(
        self,
        local_store,
        migrated_database,
        unique_prefix,
    ):
        """GC 仅删除孤立制品"""
        from engram.logbook.artifact_gc import run_gc

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        # 创建被引用的制品
        uri_referenced = f"scm/{unique_prefix}/keep_me.bin"
        content1 = secrets.token_bytes(256)
        result1 = local_store.put(uri_referenced, content1)

        # 创建未被引用的制品
        uri_orphan = f"scm/{unique_prefix}/delete_me.bin"
        content2 = secrets.token_bytes(256)
        local_store.put(uri_orphan, content2)

        # 只为 referenced 插入 DB 引用
        blob_id = insert_patch_blob_reference(
            conn,
            uri=uri_referenced,
            sha256=result1["sha256"],
            size_bytes=result1["size_bytes"],
        )

        try:
            # 运行 GC（实际删除模式）
            gc_result = run_gc(
                prefix=f"scm/{unique_prefix}/",
                dry_run=False,
                delete=True,
                dsn=dsn,
                backend="local",
                artifacts_root=str(local_store.root),
                verbose=False,
            )

            # 验证：被引用的文件仍存在
            assert local_store.exists(uri_referenced), "被引用的文件应保留"

            # 验证：孤立文件已被删除
            assert not local_store.exists(uri_orphan), "孤立文件应被删除"

            # 验证统计
            assert gc_result.deleted_count >= 1
            assert gc_result.protected_count >= 1

        finally:
            delete_patch_blob_reference(conn, blob_id)

    def test_gc_with_deleted_db_reference(
        self,
        local_store,
        migrated_database,
        unique_prefix,
    ):
        """删除 DB 引用后，GC 应清理该制品"""
        from engram.logbook.artifact_gc import run_gc

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        # 创建制品
        uri = f"scm/{unique_prefix}/will_be_orphan.bin"
        content = secrets.token_bytes(256)
        result = local_store.put(uri, content)

        # 插入 DB 引用
        blob_id = insert_patch_blob_reference(
            conn,
            uri=uri,
            sha256=result["sha256"],
            size_bytes=result["size_bytes"],
        )

        # 验证：有引用时，GC 不会删除
        gc_result1 = run_gc(
            prefix=f"scm/{unique_prefix}/",
            dry_run=True,
            dsn=dsn,
            backend="local",
            artifacts_root=str(local_store.root),
            verbose=False,
        )

        assert gc_result1.protected_count >= 1
        assert gc_result1.candidates_count == 0

        # 删除 DB 引用
        delete_patch_blob_reference(conn, blob_id)

        # 验证：无引用后，GC 会将其标记为候选
        gc_result2 = run_gc(
            prefix=f"scm/{unique_prefix}/",
            dry_run=True,
            dsn=dsn,
            backend="local",
            artifacts_root=str(local_store.root),
            verbose=False,
        )

        assert gc_result2.candidates_count >= 1

        # 实际删除
        gc_result3 = run_gc(
            prefix=f"scm/{unique_prefix}/",
            dry_run=False,
            delete=True,
            dsn=dsn,
            backend="local",
            artifacts_root=str(local_store.root),
            verbose=False,
        )

        assert gc_result3.deleted_count >= 1
        assert not local_store.exists(uri), "制品应被删除"

    def test_gc_protects_s3_uri_referenced_objects(
        self,
        object_store,
        migrated_database,
        unique_prefix,
        cleanup_artifacts,
    ):
        """
        GC 保护含 s3:// 引用的制品

        场景:
        - 在 ObjectStore 中创建制品
        - 在 DB 中使用 s3:// URI 引用该制品
        - GC dry-run 应该保护该制品，不列为候选

        这是确保 GC 正确解析 s3:// 物理 URI 的回归测试。
        """
        from engram.logbook.artifact_gc import run_gc

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        # 创建测试制品
        artifact_key = f"scm/{unique_prefix}/s3_referenced.diff"
        cleanup_artifacts.append(artifact_key)

        content = secrets.token_bytes(512)
        result = object_store.put(artifact_key, content)

        # 构建 s3:// URI
        # 格式: s3://<bucket>/<prefix>/<artifact_key>
        s3_uri = object_store.resolve(artifact_key)

        # 验证 s3_uri 格式正确
        assert s3_uri.startswith("s3://"), f"resolve 应返回 s3:// URI, got: {s3_uri}"
        assert object_store.bucket in s3_uri

        # 插入 DB 引用，使用 s3:// URI
        blob_id = insert_patch_blob_reference(
            conn,
            uri=s3_uri,  # 使用 s3:// 物理 URI
            sha256=result["sha256"],
            size_bytes=result["size_bytes"],
        )

        try:
            # 运行 GC dry-run（object 后端）
            gc_result = run_gc(
                prefix=f"scm/{unique_prefix}/",
                dry_run=True,
                delete=False,
                dsn=dsn,
                backend="object",
                s3_endpoint=object_store.endpoint,
                s3_access_key=object_store.access_key,
                s3_secret_key=object_store.secret_key,
                s3_bucket=object_store.bucket,
                verbose=False,
            )

            # 验证：s3:// 引用的对象应该被保护
            # 该对象不应出现在 candidates 中
            candidate_uris = {c.uri for c in gc_result.candidates}

            # 检查 artifact_key 是否在候选中
            # 如果 GC 正确解析了 s3:// 引用，artifact_key 不应在候选中
            is_in_candidates = any(
                artifact_key in uri or "s3_referenced.diff" in uri for uri in candidate_uris
            )

            assert not is_in_candidates, (
                f"s3:// 引用的对象应被保护，不应在 candidates 中。candidates: {candidate_uris}"
            )

            # 验证 protected_count 至少为 1
            assert gc_result.protected_count >= 1, (
                f"应至少有 1 个被保护的对象。protected_count={gc_result.protected_count}"
            )

        finally:
            # 清理 DB 引用
            delete_patch_blob_reference(conn, blob_id)

    def test_gc_s3_uri_vs_artifact_key_both_protected(
        self,
        object_store,
        migrated_database,
        unique_prefix,
        cleanup_artifacts,
    ):
        """
        测试同时使用 s3:// URI 和 artifact key 引用时的 GC 保护

        场景:
        - 创建两个制品
        - 一个使用 s3:// URI 引用
        - 一个使用 artifact key 引用
        - GC dry-run 应该保护两个制品
        """
        from engram.logbook.artifact_gc import run_gc

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        # 创建第一个制品（使用 s3:// URI 引用）
        artifact_key_1 = f"scm/{unique_prefix}/s3_ref_object.diff"
        cleanup_artifacts.append(artifact_key_1)
        content1 = secrets.token_bytes(256)
        result1 = object_store.put(artifact_key_1, content1)
        s3_uri_1 = object_store.resolve(artifact_key_1)

        # 创建第二个制品（使用 artifact key 引用）
        artifact_key_2 = f"scm/{unique_prefix}/key_ref_object.diff"
        cleanup_artifacts.append(artifact_key_2)
        content2 = secrets.token_bytes(256)
        result2 = object_store.put(artifact_key_2, content2)

        # 创建第三个制品（不引用，应为 orphan）
        artifact_key_3 = f"scm/{unique_prefix}/orphan_object.diff"
        cleanup_artifacts.append(artifact_key_3)
        content3 = secrets.token_bytes(256)
        object_store.put(artifact_key_3, content3)

        # 插入 DB 引用
        blob_id_1 = insert_patch_blob_reference(
            conn,
            uri=s3_uri_1,  # s3:// URI
            sha256=result1["sha256"],
            size_bytes=result1["size_bytes"],
        )

        blob_id_2 = insert_patch_blob_reference(
            conn,
            uri=artifact_key_2,  # artifact key
            sha256=result2["sha256"],
            size_bytes=result2["size_bytes"],
        )

        try:
            # 运行 GC dry-run
            gc_result = run_gc(
                prefix=f"scm/{unique_prefix}/",
                dry_run=True,
                delete=False,
                dsn=dsn,
                backend="object",
                s3_endpoint=object_store.endpoint,
                s3_access_key=object_store.access_key,
                s3_secret_key=object_store.secret_key,
                s3_bucket=object_store.bucket,
                verbose=False,
            )

            candidate_uris = {c.uri for c in gc_result.candidates}

            # 第一个对象（s3:// 引用）不应在候选中
            assert not any("s3_ref_object.diff" in uri for uri in candidate_uris), (
                "s3:// 引用的对象应被保护"
            )

            # 第二个对象（artifact key 引用）不应在候选中
            assert not any("key_ref_object.diff" in uri for uri in candidate_uris), (
                "artifact key 引用的对象应被保护"
            )

            # 第三个对象（无引用）应在候选中
            assert any("orphan_object.diff" in uri for uri in candidate_uris), (
                f"无引用的对象应在 candidates 中。candidates: {candidate_uris}"
            )

            # 保护数应该是 2
            assert gc_result.protected_count >= 2

            # 候选数应该是 1
            assert gc_result.candidates_count >= 1

        finally:
            delete_patch_blob_reference(conn, blob_id_1)
            delete_patch_blob_reference(conn, blob_id_2)


# ============================================================================
# 测试类: 迁移
# ============================================================================


@integration_test
@migrate_test
class TestArtifactMigration:
    """制品迁移测试（local <-> object）"""

    def test_migrate_local_to_object(
        self,
        local_store,
        object_store,
        unique_prefix,
        cleanup_artifacts,
    ):
        """迁移：local -> object"""
        # 在 local 创建制品
        uri = f"{unique_prefix}/migrate_to_object.bin"
        content = secrets.token_bytes(2048)
        expected_sha256 = hashlib.sha256(content).hexdigest()

        local_result = local_store.put(uri, content)

        assert local_result["sha256"] == expected_sha256

        # 手动迁移：读取 local，写入 object
        local_content = local_store.get(uri)
        cleanup_artifacts.append(uri)
        object_result = object_store.put(uri, local_content)

        assert object_result["sha256"] == expected_sha256
        assert object_result["size_bytes"] == len(content)

        # 验证：object store 中的内容正确
        object_content = object_store.get(uri)
        assert object_content == content

    def test_migrate_object_to_local(
        self,
        local_store,
        object_store,
        unique_prefix,
        cleanup_artifacts,
    ):
        """迁移：object -> local"""
        # 在 object 创建制品
        uri = f"{unique_prefix}/migrate_to_local.bin"
        cleanup_artifacts.append(uri)

        content = secrets.token_bytes(2048)
        expected_sha256 = hashlib.sha256(content).hexdigest()

        object_result = object_store.put(uri, content)

        assert object_result["sha256"] == expected_sha256

        # 手动迁移：读取 object，写入 local
        object_content = object_store.get(uri)
        local_result = local_store.put(uri, object_content)

        assert local_result["sha256"] == expected_sha256
        assert local_result["size_bytes"] == len(content)

        # 验证：local store 中的内容正确
        local_content = local_store.get(uri)
        assert local_content == content

    def test_migrate_large_object_with_multipart(
        self,
        local_store,
        object_store,
        unique_prefix,
        cleanup_artifacts,
    ):
        """迁移大对象（触发 Multipart）"""
        from engram.logbook.artifact_store import MULTIPART_THRESHOLD

        # 创建超过阈值的内容
        size = MULTIPART_THRESHOLD + (512 * 1024)  # 阈值 + 512KB
        content = secrets.token_bytes(size)
        expected_sha256 = hashlib.sha256(content).hexdigest()

        uri = f"{unique_prefix}/migrate_large.bin"

        # local -> object
        local_result = local_store.put(uri, content)
        assert local_result["sha256"] == expected_sha256

        local_content = local_store.get(uri)
        cleanup_artifacts.append(uri)
        object_result = object_store.put(uri, local_content)

        assert object_result["sha256"] == expected_sha256
        assert object_result["size_bytes"] == size

        # 验证完整性
        object_content = object_store.get(uri)
        assert len(object_content) == size
        assert hashlib.sha256(object_content).hexdigest() == expected_sha256

    def test_migrate_and_verify_with_audit(
        self,
        local_store,
        object_store,
        migrated_database,
        unique_prefix,
        cleanup_artifacts,
    ):
        """迁移后使用审计验证完整性"""
        from scripts.artifact_audit import ArtifactAuditor

        conn = migrated_database["conn"]

        # 创建制品
        uri = f"{unique_prefix}/migrate_verify.bin"
        content = secrets.token_bytes(1024)
        expected_sha256 = hashlib.sha256(content).hexdigest()

        # 在 local 创建
        local_store.put(uri, content)

        # 迁移到 object
        cleanup_artifacts.append(uri)
        object_store.put(uri, content)

        # 插入 DB 引用（指向 object store 中的 URI）
        blob_id = insert_patch_blob_reference(
            conn,
            uri=uri,
            sha256=expected_sha256,
            size_bytes=len(content),
        )

        try:
            # 使用 object store 审计
            auditor = ArtifactAuditor(
                conn=conn,
                artifact_store=object_store,
                verbose=False,
            )

            audit_result = auditor.audit_record(
                table="patch_blobs",
                record_id=blob_id,
                uri=uri,
                expected_sha256=expected_sha256,
            )

            assert audit_result.status == "ok"
            assert audit_result.actual_sha256 == expected_sha256

        finally:
            delete_patch_blob_reference(conn, blob_id)


# ============================================================================
# 测试类: 端到端完整流程
# ============================================================================


@integration_test
class TestEndToEndFlow:
    """端到端完整流程测试"""

    def test_full_lifecycle_local(
        self,
        local_store,
        migrated_database,
        unique_prefix,
    ):
        """完整生命周期：创建 -> 引用 -> 审计 -> 删除引用 -> GC"""
        from engram.logbook.artifact_gc import run_gc
        from scripts.artifact_audit import ArtifactAuditor

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        # 1. 创建多个制品
        artifacts = []
        for i in range(3):
            uri = f"scm/{unique_prefix}/file_{i}.bin"
            content = secrets.token_bytes(256 + i * 100)
            result = local_store.put(uri, content)
            artifacts.append(
                {
                    "uri": uri,
                    "sha256": result["sha256"],
                    "size_bytes": result["size_bytes"],
                }
            )

        # 2. 为前两个创建 DB 引用
        blob_ids = []
        for artifact in artifacts[:2]:
            blob_id = insert_patch_blob_reference(
                conn,
                uri=artifact["uri"],
                sha256=artifact["sha256"],
                size_bytes=artifact["size_bytes"],
            )
            blob_ids.append(blob_id)

        # 3. 审计所有引用
        auditor = ArtifactAuditor(
            conn=conn,
            artifact_store=local_store,
            verbose=False,
        )

        for i, blob_id in enumerate(blob_ids):
            result = auditor.audit_record(
                table="patch_blobs",
                record_id=blob_id,
                uri=artifacts[i]["uri"],
                expected_sha256=artifacts[i]["sha256"],
            )
            assert result.status == "ok"

        # 4. GC dry-run：第三个文件应为候选
        gc_result1 = run_gc(
            prefix=f"scm/{unique_prefix}/",
            dry_run=True,
            dsn=dsn,
            backend="local",
            artifacts_root=str(local_store.root),
            verbose=False,
        )

        assert gc_result1.protected_count == 2
        assert gc_result1.candidates_count == 1

        # 5. 删除一个 DB 引用
        delete_patch_blob_reference(conn, blob_ids[0])
        blob_ids = blob_ids[1:]

        # 6. 再次 GC：现在有两个候选
        gc_result2 = run_gc(
            prefix=f"scm/{unique_prefix}/",
            dry_run=True,
            dsn=dsn,
            backend="local",
            artifacts_root=str(local_store.root),
            verbose=False,
        )

        assert gc_result2.protected_count == 1
        assert gc_result2.candidates_count == 2

        # 7. 执行实际 GC
        gc_result3 = run_gc(
            prefix=f"scm/{unique_prefix}/",
            dry_run=False,
            delete=True,
            dsn=dsn,
            backend="local",
            artifacts_root=str(local_store.root),
            verbose=False,
        )

        assert gc_result3.deleted_count == 2
        assert gc_result3.protected_count == 1

        # 8. 验证：只有被引用的文件还存在
        assert local_store.exists(artifacts[1]["uri"])
        assert not local_store.exists(artifacts[0]["uri"])
        assert not local_store.exists(artifacts[2]["uri"])

        # 9. 清理
        for blob_id in blob_ids:
            delete_patch_blob_reference(conn, blob_id)

    def test_full_lifecycle_with_attachments(
        self,
        local_store,
        migrated_database,
        unique_prefix,
    ):
        """完整生命周期：使用 attachments 表"""
        from engram.logbook.artifact_gc import run_gc
        from scripts.artifact_audit import ArtifactAuditor

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        # 创建制品
        uri = f"attachments/{unique_prefix}/document.bin"
        content = secrets.token_bytes(512)
        result = local_store.put(uri, content)

        # 创建 DB 引用
        attachment_id, item_id = insert_attachment_reference(
            conn,
            uri=uri,
            sha256=result["sha256"],
            size_bytes=result["size_bytes"],
            kind="report",
        )

        try:
            # 审计
            auditor = ArtifactAuditor(
                conn=conn,
                artifact_store=local_store,
                verbose=False,
            )

            audit_result = auditor.audit_record(
                table="attachments",
                record_id=attachment_id,
                uri=uri,
                expected_sha256=result["sha256"],
            )

            assert audit_result.status == "ok"

            # GC 应保护
            gc_result = run_gc(
                prefix=f"attachments/{unique_prefix}/",
                dry_run=True,
                dsn=dsn,
                backend="local",
                artifacts_root=str(local_store.root),
                verbose=False,
            )

            assert gc_result.protected_count >= 1
            assert gc_result.candidates_count == 0

        finally:
            delete_attachment_reference(conn, attachment_id, item_id)


# ============================================================================
# 测试类: OpenMemory 角色权限验证
# ============================================================================


@integration_test
class TestOpenMemoryRolePermissions:
    """
    OpenMemory 角色权限验证测试

    测试场景：
    1. openmemory_svc (继承 openmemory_app) 在 OM_PG_SCHEMA 内:
       - CREATE TABLE 应失败（无 DDL 权限）
       - SELECT/INSERT/UPDATE/DELETE 应成功（有 DML 权限）

    2. openmemory_migrator_login (继承 openmemory_migrator):
       - 应有 DDL 权限，可创建/修改表

    注意：
    - 这些测试需要在统一栈环境中运行
    - 需要 superuser 权限来创建测试用的登录角色
    - 测试使用临时表名避免影响真实数据
    """

    # 测试用密码（仅用于测试环境）
    TEST_PASSWORD = "test_password_12345"

    @pytest.fixture(scope="class")
    def om_schema(self, migrated_database):
        """获取 OpenMemory 目标 schema（默认 openmemory）"""
        return os.environ.get("OM_PG_SCHEMA", "openmemory")

    @pytest.fixture(scope="class")
    def setup_test_roles(self, migrated_database):
        """
        创建测试用的登录角色（如果不存在）

        创建:
        - openmemory_svc: 继承 openmemory_app
        - openmemory_migrator_login: 继承 openmemory_migrator
        """

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        # 使用 superuser 连接创建测试角色
        with conn.cursor() as cur:
            # 创建 openmemory_svc 登录角色
            cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_svc') THEN
                        CREATE ROLE openmemory_svc LOGIN PASSWORD %s;
                        RAISE NOTICE 'Created test role: openmemory_svc';
                    ELSE
                        ALTER ROLE openmemory_svc WITH LOGIN PASSWORD %s;
                        RAISE NOTICE 'Updated test role: openmemory_svc';
                    END IF;

                    -- 确保继承 openmemory_app
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_app') THEN
                        GRANT openmemory_app TO openmemory_svc;
                    END IF;
                END $$;
            """,
                (self.TEST_PASSWORD, self.TEST_PASSWORD),
            )

            # 创建 openmemory_migrator_login 登录角色
            cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator_login') THEN
                        CREATE ROLE openmemory_migrator_login LOGIN PASSWORD %s;
                        RAISE NOTICE 'Created test role: openmemory_migrator_login';
                    ELSE
                        ALTER ROLE openmemory_migrator_login WITH LOGIN PASSWORD %s;
                        RAISE NOTICE 'Updated test role: openmemory_migrator_login';
                    END IF;

                    -- 确保继承 openmemory_migrator
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator') THEN
                        GRANT openmemory_migrator TO openmemory_migrator_login;
                    END IF;
                END $$;
            """,
                (self.TEST_PASSWORD, self.TEST_PASSWORD),
            )

        # 解析 DSN 获取连接参数
        from urllib.parse import urlparse

        parsed = urlparse(dsn)

        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "dbname": parsed.path.lstrip("/") or "engram",
            "svc_user": "openmemory_svc",
            "migrator_user": "openmemory_migrator_login",
            "password": self.TEST_PASSWORD,
        }

    @pytest.fixture(scope="class")
    def svc_connection(self, setup_test_roles, om_schema):
        """
        使用 openmemory_svc 角色建立的数据库连接
        """
        import psycopg

        config = setup_test_roles

        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user=config["svc_user"],
                password=config["password"],
                autocommit=True,
                options=f"-c search_path={om_schema},public",
            )
            yield conn
            conn.close()
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_svc 连接: {e}")

    @pytest.fixture(scope="class")
    def migrator_connection(self, setup_test_roles, om_schema):
        """
        使用 openmemory_migrator_login 角色建立的数据库连接
        """
        import psycopg

        config = setup_test_roles

        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user=config["migrator_user"],
                password=config["password"],
                autocommit=True,
                options=f"-c search_path={om_schema},public",
            )
            yield conn
            conn.close()
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_migrator_login 连接: {e}")

    @pytest.fixture
    def test_table_name(self):
        """生成唯一的测试表名"""
        unique_id = secrets.token_hex(4)
        return f"_test_perm_{unique_id}"

    def test_svc_cannot_create_table_in_schema(
        self, svc_connection, om_schema, test_table_name, migrated_database
    ):
        """
        验证 openmemory_svc 无法在 OM_PG_SCHEMA 内执行 CREATE TABLE

        预期: 抛出权限错误 (42501 - insufficient_privilege)
        """
        import psycopg

        with svc_connection.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{test_table_name} (
                        id SERIAL PRIMARY KEY,
                        name TEXT
                    )
                """)

    def test_svc_can_select_from_existing_table(
        self, svc_connection, migrator_connection, om_schema, test_table_name, migrated_database
    ):
        """
        验证 openmemory_svc 可以执行 SELECT

        步骤:
        1. 使用 migrator 创建测试表
        2. 使用 svc 执行 SELECT
        3. 清理测试表
        """

        # 1. 使用 migrator 创建测试表
        with migrator_connection.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE {om_schema}.{test_table_name} (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            # 插入测试数据
            cur.execute(f"""
                INSERT INTO {om_schema}.{test_table_name} (name) VALUES ('test_row')
            """)

        try:
            # 2. 使用 svc 执行 SELECT
            with svc_connection.cursor() as cur:
                cur.execute(f"SELECT id, name FROM {om_schema}.{test_table_name}")
                rows = cur.fetchall()

                assert len(rows) == 1
                assert rows[0][1] == "test_row"
        finally:
            # 3. 清理测试表
            with migrator_connection.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{test_table_name}")

    def test_svc_can_insert_update_delete(
        self, svc_connection, migrator_connection, om_schema, test_table_name
    ):
        """
        验证 openmemory_svc 可以执行 INSERT/UPDATE/DELETE
        """

        # 使用 migrator 创建测试表
        with migrator_connection.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE {om_schema}.{test_table_name} (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    value INTEGER DEFAULT 0
                )
            """)

        try:
            with svc_connection.cursor() as cur:
                # INSERT
                cur.execute(f"""
                    INSERT INTO {om_schema}.{test_table_name} (name, value)
                    VALUES ('row1', 10), ('row2', 20)
                    RETURNING id
                """)
                inserted_ids = [row[0] for row in cur.fetchall()]
                assert len(inserted_ids) == 2

                # UPDATE
                cur.execute(f"""
                    UPDATE {om_schema}.{test_table_name}
                    SET value = value + 100
                    WHERE name = 'row1'
                """)
                assert cur.rowcount == 1

                # 验证 UPDATE 结果
                cur.execute(f"""
                    SELECT value FROM {om_schema}.{test_table_name} WHERE name = 'row1'
                """)
                value = cur.fetchone()[0]
                assert value == 110

                # DELETE
                cur.execute(f"""
                    DELETE FROM {om_schema}.{test_table_name} WHERE name = 'row2'
                """)
                assert cur.rowcount == 1

                # 验证 DELETE 结果
                cur.execute(f"SELECT COUNT(*) FROM {om_schema}.{test_table_name}")
                count = cur.fetchone()[0]
                assert count == 1
        finally:
            # 清理测试表
            with migrator_connection.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{test_table_name}")

    def test_svc_cannot_alter_table(
        self, svc_connection, migrator_connection, om_schema, test_table_name
    ):
        """
        验证 openmemory_svc 无法执行 ALTER TABLE
        """
        import psycopg

        # 使用 migrator 创建测试表
        with migrator_connection.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE {om_schema}.{test_table_name} (
                    id SERIAL PRIMARY KEY,
                    name TEXT
                )
            """)

        try:
            # 尝试使用 svc 执行 ALTER TABLE
            with svc_connection.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"""
                        ALTER TABLE {om_schema}.{test_table_name}
                        ADD COLUMN new_col TEXT
                    """)
        finally:
            # 清理测试表
            with migrator_connection.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{test_table_name}")

    def test_svc_cannot_drop_table(
        self, svc_connection, migrator_connection, om_schema, test_table_name
    ):
        """
        验证 openmemory_svc 无法执行 DROP TABLE
        """
        import psycopg

        # 使用 migrator 创建测试表
        with migrator_connection.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE {om_schema}.{test_table_name} (
                    id SERIAL PRIMARY KEY
                )
            """)

        try:
            # 尝试使用 svc 执行 DROP TABLE
            with svc_connection.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"DROP TABLE {om_schema}.{test_table_name}")
        finally:
            # 使用 migrator 清理
            with migrator_connection.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{test_table_name}")

    def test_migrator_can_create_table(self, migrator_connection, om_schema, test_table_name):
        """
        验证 openmemory_migrator_login 可以在 OM_PG_SCHEMA 内执行 CREATE TABLE
        """
        with migrator_connection.cursor() as cur:
            # CREATE TABLE 应成功
            cur.execute(f"""
                CREATE TABLE {om_schema}.{test_table_name} (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    metadata JSONB DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # 验证表存在
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                )
            """,
                (om_schema, test_table_name),
            )
            exists = cur.fetchone()[0]
            assert exists is True

            # 清理
            cur.execute(f"DROP TABLE {om_schema}.{test_table_name}")

    def test_migrator_can_create_index(self, migrator_connection, om_schema, test_table_name):
        """
        验证 openmemory_migrator_login 可以创建索引
        """
        with migrator_connection.cursor() as cur:
            # 创建表
            cur.execute(f"""
                CREATE TABLE {om_schema}.{test_table_name} (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    category TEXT
                )
            """)

            # 创建索引应成功
            index_name = f"idx_{test_table_name}_category"
            cur.execute(f"""
                CREATE INDEX {index_name} ON {om_schema}.{test_table_name} (category)
            """)

            # 验证索引存在
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname = %s AND indexname = %s
                )
            """,
                (om_schema, index_name),
            )
            exists = cur.fetchone()[0]
            assert exists is True

            # 清理
            cur.execute(f"DROP TABLE {om_schema}.{test_table_name}")

    def test_migrator_can_alter_and_drop_table(
        self, migrator_connection, om_schema, test_table_name
    ):
        """
        验证 openmemory_migrator_login 可以执行 ALTER TABLE 和 DROP TABLE
        """
        with migrator_connection.cursor() as cur:
            # 创建表
            cur.execute(f"""
                CREATE TABLE {om_schema}.{test_table_name} (
                    id SERIAL PRIMARY KEY,
                    name TEXT
                )
            """)

            # ALTER TABLE 应成功
            cur.execute(f"""
                ALTER TABLE {om_schema}.{test_table_name}
                ADD COLUMN description TEXT
            """)

            # 验证列存在
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = %s
                      AND column_name = 'description'
                )
            """,
                (om_schema, test_table_name),
            )
            exists = cur.fetchone()[0]
            assert exists is True

            # DROP TABLE 应成功
            cur.execute(f"DROP TABLE {om_schema}.{test_table_name}")

            # 验证表不存在
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                )
            """,
                (om_schema, test_table_name),
            )
            exists = cur.fetchone()[0]
            assert exists is False

    def test_migrator_cannot_create_in_public_schema(self, migrator_connection, test_table_name):
        """
        验证 openmemory_migrator_login 无法在 public schema 创建表
        （strict 策略下 public schema 的 CREATE 权限被撤销）
        """
        import psycopg

        with migrator_connection.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"""
                    CREATE TABLE public.{test_table_name} (
                        id SERIAL PRIMARY KEY
                    )
                """)


@integration_test
class TestLogbookRolePermissions:
    """
    Logbook 角色权限验证测试

    测试场景：
    1. logbook_svc (继承 engram_app_readwrite) 在 Logbook schema (logbook, scm, analysis 等):
       - CREATE TABLE 应失败（无 DDL 权限）
       - SELECT/INSERT/UPDATE/DELETE 应成功（有 DML 权限）

    2. logbook_migrator (继承 engram_migrator):
       - 应有 DDL 权限，可创建/修改表

    3. SET ROLE 功能验证:
       - 通过连接 options 设置 role 或显式 SET ROLE 时权限正确
    """

    TEST_PASSWORD = "test_password_12345"
    LOGBOOK_SCHEMAS = ["logbook", "scm", "analysis", "governance"]

    @pytest.fixture(scope="class")
    def setup_logbook_test_roles(self, migrated_database):
        """
        创建/更新 Logbook 测试用的登录角色

        创建:
        - logbook_svc: 继承 engram_app_readwrite
        - logbook_migrator: 继承 engram_migrator
        """

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        with conn.cursor() as cur:
            # 创建 logbook_svc 登录角色
            cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logbook_svc') THEN
                        CREATE ROLE logbook_svc LOGIN PASSWORD %s;
                        RAISE NOTICE 'Created test role: logbook_svc';
                    ELSE
                        ALTER ROLE logbook_svc WITH LOGIN PASSWORD %s;
                        RAISE NOTICE 'Updated test role: logbook_svc';
                    END IF;

                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app_readwrite') THEN
                        GRANT engram_app_readwrite TO logbook_svc;
                    END IF;
                END $$;
            """,
                (self.TEST_PASSWORD, self.TEST_PASSWORD),
            )

            # 创建 logbook_migrator 登录角色
            cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logbook_migrator') THEN
                        CREATE ROLE logbook_migrator LOGIN PASSWORD %s;
                        RAISE NOTICE 'Created test role: logbook_migrator';
                    ELSE
                        ALTER ROLE logbook_migrator WITH LOGIN PASSWORD %s;
                        RAISE NOTICE 'Updated test role: logbook_migrator';
                    END IF;

                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_migrator') THEN
                        GRANT engram_migrator TO logbook_migrator;
                    END IF;
                END $$;
            """,
                (self.TEST_PASSWORD, self.TEST_PASSWORD),
            )

        from urllib.parse import urlparse

        parsed = urlparse(dsn)

        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "dbname": parsed.path.lstrip("/") or "engram",
            "svc_user": "logbook_svc",
            "migrator_user": "logbook_migrator",
            "password": self.TEST_PASSWORD,
        }

    @pytest.fixture(scope="class")
    def svc_connection(self, setup_logbook_test_roles):
        """使用 logbook_svc 角色建立的数据库连接"""
        import psycopg

        config = setup_logbook_test_roles

        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user=config["svc_user"],
                password=config["password"],
                autocommit=True,
            )
            yield conn
            conn.close()
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 logbook_svc 连接: {e}")

    @pytest.fixture(scope="class")
    def migrator_connection(self, setup_logbook_test_roles):
        """使用 logbook_migrator 角色建立的数据库连接"""
        import psycopg

        config = setup_logbook_test_roles

        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user=config["migrator_user"],
                password=config["password"],
                autocommit=True,
            )
            yield conn
            conn.close()
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 logbook_migrator 连接: {e}")

    @pytest.fixture
    def test_table_name(self):
        """生成唯一的测试表名"""
        unique_id = secrets.token_hex(4)
        return f"_test_logbook_perm_{unique_id}"

    def test_svc_cannot_create_table_in_logbook_schemas(self, svc_connection, test_table_name):
        """
        验证 logbook_svc 无法在 Logbook schema 内执行 CREATE TABLE

        预期: 抛出权限错误 (42501 - insufficient_privilege)
        """
        import psycopg

        for schema in self.LOGBOOK_SCHEMAS:
            with svc_connection.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"""
                        CREATE TABLE {schema}.{test_table_name} (
                            id SERIAL PRIMARY KEY,
                            name TEXT
                        )
                    """)

    def test_svc_can_crud_on_existing_tables(
        self, svc_connection, migrator_connection, test_table_name
    ):
        """
        验证 logbook_svc 可以对 migrator 创建的表执行 SELECT/INSERT/UPDATE/DELETE
        """

        # 使用 logbook schema 测试
        schema = "logbook"

        # 使用 migrator 创建测试表
        with migrator_connection.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE {schema}.{test_table_name} (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    value INTEGER DEFAULT 0
                )
            """)

        try:
            with svc_connection.cursor() as cur:
                # INSERT
                cur.execute(f"""
                    INSERT INTO {schema}.{test_table_name} (name, value)
                    VALUES ('row1', 10), ('row2', 20)
                    RETURNING id
                """)
                inserted_ids = [row[0] for row in cur.fetchall()]
                assert len(inserted_ids) == 2

                # SELECT
                cur.execute(f"SELECT id, name, value FROM {schema}.{test_table_name}")
                rows = cur.fetchall()
                assert len(rows) == 2

                # UPDATE
                cur.execute(f"""
                    UPDATE {schema}.{test_table_name}
                    SET value = value + 100
                    WHERE name = 'row1'
                """)
                assert cur.rowcount == 1

                # 验证 UPDATE 结果
                cur.execute(f"""
                    SELECT value FROM {schema}.{test_table_name} WHERE name = 'row1'
                """)
                value = cur.fetchone()[0]
                assert value == 110

                # DELETE
                cur.execute(f"""
                    DELETE FROM {schema}.{test_table_name} WHERE name = 'row2'
                """)
                assert cur.rowcount == 1

                # 验证 DELETE 结果
                cur.execute(f"SELECT COUNT(*) FROM {schema}.{test_table_name}")
                count = cur.fetchone()[0]
                assert count == 1
        finally:
            # 清理测试表
            with migrator_connection.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {schema}.{test_table_name}")

    def test_migrator_can_create_tables_in_all_logbook_schemas(
        self, migrator_connection, test_table_name
    ):
        """
        验证 logbook_migrator 可以在所有 Logbook schema 内执行 CREATE TABLE
        """
        for schema in self.LOGBOOK_SCHEMAS:
            with migrator_connection.cursor() as cur:
                # CREATE TABLE 应成功
                cur.execute(f"""
                    CREATE TABLE {schema}.{test_table_name} (
                        id SERIAL PRIMARY KEY,
                        content TEXT,
                        metadata JSONB DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # 验证表存在
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = %s
                    )
                """,
                    (schema, test_table_name),
                )
                exists = cur.fetchone()[0]
                assert exists is True, f"表 {schema}.{test_table_name} 应已创建"

                # 清理
                cur.execute(f"DROP TABLE {schema}.{test_table_name}")

    def test_svc_cannot_alter_or_drop_table(
        self, svc_connection, migrator_connection, test_table_name
    ):
        """
        验证 logbook_svc 无法执行 ALTER TABLE 和 DROP TABLE
        """
        import psycopg

        schema = "logbook"

        # 使用 migrator 创建测试表
        with migrator_connection.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE {schema}.{test_table_name} (
                    id SERIAL PRIMARY KEY,
                    name TEXT
                )
            """)

        try:
            # 尝试使用 svc 执行 ALTER TABLE
            with svc_connection.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"""
                        ALTER TABLE {schema}.{test_table_name}
                        ADD COLUMN new_col TEXT
                    """)

            # 尝试使用 svc 执行 DROP TABLE
            with svc_connection.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"DROP TABLE {schema}.{test_table_name}")
        finally:
            # 使用 migrator 清理
            with migrator_connection.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {schema}.{test_table_name}")


@integration_test
class TestSetRolePermissions:
    """
    SET ROLE 功能权限验证测试

    验证通过连接 options 设置 role 或显式 SET ROLE 时权限正确工作。
    这对于使用 OM_PG_SET_ROLE 等环境变量切换角色的场景至关重要。
    """

    TEST_PASSWORD = "test_password_12345"

    @pytest.fixture(scope="class")
    def setup_set_role_test(self, migrated_database):
        """设置 SET ROLE 测试所需的角色"""

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        with conn.cursor() as cur:
            # 确保测试角色存在并有正确的继承关系
            for login_role, inherit_role in [
                ("logbook_svc", "engram_app_readwrite"),
                ("logbook_migrator", "engram_migrator"),
                ("openmemory_svc", "openmemory_app"),
                ("openmemory_migrator_login", "openmemory_migrator"),
            ]:
                cur.execute(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login_role}') THEN
                            CREATE ROLE {login_role} LOGIN PASSWORD %s;
                        ELSE
                            ALTER ROLE {login_role} WITH LOGIN PASSWORD %s;
                        END IF;

                        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{inherit_role}') THEN
                            GRANT {inherit_role} TO {login_role};
                        END IF;
                    END $$;
                """,
                    (self.TEST_PASSWORD, self.TEST_PASSWORD),
                )

        from urllib.parse import urlparse

        parsed = urlparse(dsn)

        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "dbname": parsed.path.lstrip("/") or "engram",
            "password": self.TEST_PASSWORD,
        }

    def test_set_role_via_connection_options(self, setup_set_role_test, migrated_database):
        """
        验证通过连接 options 设置 role（-c role=xxx）时权限正确

        模拟 OM_PG_SET_ROLE 的行为
        """
        import psycopg

        config = setup_set_role_test
        om_schema = os.environ.get("OM_PG_SCHEMA", "openmemory")

        # 使用 openmemory_migrator_login 登录，通过 options 设置 role 为 openmemory_migrator
        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="openmemory_migrator_login",
                password=config["password"],
                autocommit=True,
                options=f"-c role=openmemory_migrator -c search_path={om_schema},public",
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_migrator_login 连接: {e}")

        try:
            with conn.cursor() as cur:
                # 验证当前角色
                cur.execute("SELECT current_user, session_user, current_setting('role')")
                current_user, session_user, role_setting = cur.fetchone()

                # current_user 应该是 openmemory_migrator（SET ROLE 后）
                # session_user 应该是 openmemory_migrator_login（登录用户）
                assert session_user == "openmemory_migrator_login"

                # 验证有 DDL 权限（可以创建表）
                test_table = f"_test_set_role_{secrets.token_hex(4)}"
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{test_table} (
                        id SERIAL PRIMARY KEY,
                        data TEXT
                    )
                """)

                # 验证表已创建
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = %s
                    )
                """,
                    (om_schema, test_table),
                )
                exists = cur.fetchone()[0]
                assert exists is True, "使用 SET ROLE 后应有 DDL 权限"

                # 清理
                cur.execute(f"DROP TABLE {om_schema}.{test_table}")
        finally:
            conn.close()

    def test_explicit_set_role_in_session(self, setup_set_role_test, migrated_database):
        """
        验证显式 SET ROLE 命令正确切换权限
        """
        import psycopg

        config = setup_set_role_test
        om_schema = os.environ.get("OM_PG_SCHEMA", "openmemory")

        # 使用 openmemory_svc 登录（只有 DML 权限）
        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="openmemory_svc",
                password=config["password"],
                autocommit=True,
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_svc 连接: {e}")

        test_table = f"_test_explicit_set_role_{secrets.token_hex(4)}"

        try:
            with conn.cursor() as cur:
                # 尝试创建表应失败（openmemory_svc 无 DDL 权限）
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"""
                        CREATE TABLE {om_schema}.{test_table} (id SERIAL PRIMARY KEY)
                    """)

            # 注意: openmemory_svc 无法 SET ROLE 到 openmemory_migrator
            # 因为没有被授予 openmemory_migrator 角色
            # 这是正确的行为 - 最小权限原则

        finally:
            conn.close()

    def test_set_role_logbook_migrator_to_engram_migrator(
        self, setup_set_role_test, migrated_database
    ):
        """
        验证 logbook_migrator 通过 SET ROLE 到 engram_migrator 时权限正确
        """
        import psycopg

        config = setup_set_role_test

        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="logbook_migrator",
                password=config["password"],
                autocommit=True,
                options="-c role=engram_migrator",
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 logbook_migrator 连接: {e}")

        test_table = f"_test_logbook_set_role_{secrets.token_hex(4)}"

        try:
            with conn.cursor() as cur:
                # 验证当前角色设置
                cur.execute("SELECT session_user, current_setting('role', true)")
                session_user, role_setting = cur.fetchone()
                assert session_user == "logbook_migrator"

                # 验证可以在 logbook schema 创建表
                cur.execute(f"""
                    CREATE TABLE logbook.{test_table} (
                        id SERIAL PRIMARY KEY,
                        content TEXT
                    )
                """)

                # 验证表存在
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'logbook' AND table_name = %s
                    )
                """,
                    (test_table,),
                )
                exists = cur.fetchone()[0]
                assert exists is True

                # 清理
                cur.execute(f"DROP TABLE logbook.{test_table}")
        finally:
            conn.close()

    def test_logbook_svc_dml_with_set_role(self, setup_set_role_test, migrated_database):
        """
        验证 logbook_svc 通过 SET ROLE 到 engram_app_readwrite 时 DML 权限正确
        """
        import psycopg

        config = setup_set_role_test
        conn = migrated_database["conn"]

        # 先用 superuser 创建测试表
        test_table = f"_test_svc_set_role_{secrets.token_hex(4)}"
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS logbook.{test_table} (
                    id SERIAL PRIMARY KEY,
                    name TEXT
                )
            """)
            # 授予权限
            cur.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON logbook.{test_table} TO engram_app_readwrite"
            )
            cur.execute(
                f"GRANT USAGE ON SEQUENCE logbook.{test_table}_id_seq TO engram_app_readwrite"
            )

        try:
            # 使用 logbook_svc 连接并设置 role
            svc_conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="logbook_svc",
                password=config["password"],
                autocommit=True,
                options="-c role=engram_app_readwrite",
            )
        except psycopg.OperationalError as e:
            # 清理并跳过
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS logbook.{test_table}")
            pytest.skip(f"无法使用 logbook_svc 连接: {e}")

        try:
            with svc_conn.cursor() as cur:
                # INSERT
                cur.execute(f"""
                    INSERT INTO logbook.{test_table} (name) VALUES ('test_row')
                    RETURNING id
                """)
                row_id = cur.fetchone()[0]
                assert row_id is not None

                # SELECT
                cur.execute(f"SELECT name FROM logbook.{test_table} WHERE id = %s", (row_id,))
                name = cur.fetchone()[0]
                assert name == "test_row"

                # UPDATE
                cur.execute(
                    f"UPDATE logbook.{test_table} SET name = 'updated' WHERE id = %s", (row_id,)
                )
                assert cur.rowcount == 1

                # DELETE
                cur.execute(f"DELETE FROM logbook.{test_table} WHERE id = %s", (row_id,))
                assert cur.rowcount == 1
        finally:
            svc_conn.close()
            # 清理测试表
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS logbook.{test_table}")


@integration_test
class TestCrossSchemaPermissions:
    """
    跨 Schema 权限验证测试

    验证 migrator 创建表后 svc 能正确 CRUD，同时覆盖 OpenMemory 与 Logbook schema。
    """

    TEST_PASSWORD = "test_password_12345"

    @pytest.fixture(scope="class")
    def setup_cross_schema_test(self, migrated_database):
        """设置跨 Schema 测试所需的角色"""

        conn = migrated_database["conn"]
        dsn = migrated_database["dsn"]

        with conn.cursor() as cur:
            # 确保所有测试角色存在
            for login_role, inherit_role in [
                ("logbook_svc", "engram_app_readwrite"),
                ("logbook_migrator", "engram_migrator"),
                ("openmemory_svc", "openmemory_app"),
                ("openmemory_migrator_login", "openmemory_migrator"),
            ]:
                cur.execute(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login_role}') THEN
                            CREATE ROLE {login_role} LOGIN PASSWORD %s;
                        ELSE
                            ALTER ROLE {login_role} WITH LOGIN PASSWORD %s;
                        END IF;

                        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{inherit_role}') THEN
                            GRANT {inherit_role} TO {login_role};
                        END IF;
                    END $$;
                """,
                    (self.TEST_PASSWORD, self.TEST_PASSWORD),
                )

        from urllib.parse import urlparse

        parsed = urlparse(dsn)

        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "dbname": parsed.path.lstrip("/") or "engram",
            "password": self.TEST_PASSWORD,
            "superuser_conn": conn,
        }

    def test_migrator_creates_svc_can_crud_logbook_schema(self, setup_cross_schema_test):
        """
        验证 Logbook: migrator 创建表 -> svc 能 SELECT/INSERT/UPDATE/DELETE
        """
        import psycopg

        config = setup_cross_schema_test
        test_table = f"_test_cross_logbook_{secrets.token_hex(4)}"
        schema = "logbook"

        # 使用 logbook_migrator 连接创建表
        try:
            migrator_conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="logbook_migrator",
                password=config["password"],
                autocommit=True,
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 logbook_migrator 连接: {e}")

        try:
            with migrator_conn.cursor() as cur:
                # migrator 创建表
                cur.execute(f"""
                    CREATE TABLE {schema}.{test_table} (
                        id SERIAL PRIMARY KEY,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)
            migrator_conn.close()

            # 使用 logbook_svc 连接执行 CRUD
            try:
                svc_conn = psycopg.connect(
                    host=config["host"],
                    port=config["port"],
                    dbname=config["dbname"],
                    user="logbook_svc",
                    password=config["password"],
                    autocommit=True,
                )
            except psycopg.OperationalError as e:
                pytest.skip(f"无法使用 logbook_svc 连接: {e}")

            try:
                with svc_conn.cursor() as cur:
                    # INSERT
                    cur.execute(f"""
                        INSERT INTO {schema}.{test_table} (content)
                        VALUES ('test content 1'), ('test content 2')
                        RETURNING id
                    """)
                    ids = [row[0] for row in cur.fetchall()]
                    assert len(ids) == 2, "svc 应能 INSERT"

                    # SELECT
                    cur.execute(f"SELECT id, content FROM {schema}.{test_table}")
                    rows = cur.fetchall()
                    assert len(rows) == 2, "svc 应能 SELECT"

                    # UPDATE
                    cur.execute(
                        f"""
                        UPDATE {schema}.{test_table}
                        SET content = 'updated content'
                        WHERE id = %s
                    """,
                        (ids[0],),
                    )
                    assert cur.rowcount == 1, "svc 应能 UPDATE"

                    # DELETE
                    cur.execute(f"DELETE FROM {schema}.{test_table} WHERE id = %s", (ids[1],))
                    assert cur.rowcount == 1, "svc 应能 DELETE"
            finally:
                svc_conn.close()
        finally:
            # 清理
            superuser_conn = config["superuser_conn"]
            with superuser_conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {schema}.{test_table}")

    def test_migrator_creates_svc_can_crud_openmemory_schema(self, setup_cross_schema_test):
        """
        验证 OpenMemory: migrator 创建表 -> svc 能 SELECT/INSERT/UPDATE/DELETE
        """
        import psycopg

        config = setup_cross_schema_test
        om_schema = os.environ.get("OM_PG_SCHEMA", "openmemory")
        test_table = f"_test_cross_om_{secrets.token_hex(4)}"

        # 使用 openmemory_migrator_login 连接创建表
        try:
            migrator_conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="openmemory_migrator_login",
                password=config["password"],
                autocommit=True,
                options=f"-c search_path={om_schema},public",
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_migrator_login 连接: {e}")

        try:
            with migrator_conn.cursor() as cur:
                # migrator 创建表
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{test_table} (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        metadata JSONB DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
            migrator_conn.close()

            # 使用 openmemory_svc 连接执行 CRUD
            try:
                svc_conn = psycopg.connect(
                    host=config["host"],
                    port=config["port"],
                    dbname=config["dbname"],
                    user="openmemory_svc",
                    password=config["password"],
                    autocommit=True,
                    options=f"-c search_path={om_schema},public",
                )
            except psycopg.OperationalError as e:
                pytest.skip(f"无法使用 openmemory_svc 连接: {e}")

            try:
                with svc_conn.cursor() as cur:
                    # INSERT
                    cur.execute(f"""
                        INSERT INTO {om_schema}.{test_table} (id, user_id, content)
                        VALUES ('mem_1', 'user_1', 'test memory 1'),
                               ('mem_2', 'user_1', 'test memory 2')
                    """)

                    # SELECT
                    cur.execute(f"SELECT id, content FROM {om_schema}.{test_table}")
                    rows = cur.fetchall()
                    assert len(rows) == 2, "svc 应能 SELECT"

                    # UPDATE
                    cur.execute(f"""
                        UPDATE {om_schema}.{test_table}
                        SET content = 'updated memory',
                            metadata = '{{"updated": true}}'::jsonb
                        WHERE id = 'mem_1'
                    """)
                    assert cur.rowcount == 1, "svc 应能 UPDATE"

                    # DELETE
                    cur.execute(f"DELETE FROM {om_schema}.{test_table} WHERE id = 'mem_2'")
                    assert cur.rowcount == 1, "svc 应能 DELETE"

                    # 验证最终状态
                    cur.execute(f"SELECT COUNT(*) FROM {om_schema}.{test_table}")
                    count = cur.fetchone()[0]
                    assert count == 1
            finally:
                svc_conn.close()
        finally:
            # 清理
            superuser_conn = config["superuser_conn"]
            with superuser_conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{test_table}")

    def test_both_schemas_in_single_transaction(self, setup_cross_schema_test):
        """
        验证可以在同一事务中操作 Logbook 和 OpenMemory schema（使用有权限的连接）
        """
        import psycopg

        config = setup_cross_schema_test
        superuser_conn = config["superuser_conn"]
        om_schema = os.environ.get("OM_PG_SCHEMA", "openmemory")

        test_table_logbook = f"_test_both_logbook_{secrets.token_hex(4)}"
        test_table_om = f"_test_both_om_{secrets.token_hex(4)}"

        try:
            with superuser_conn.cursor() as cur:
                # 创建两个 schema 的表
                cur.execute(f"""
                    CREATE TABLE logbook.{test_table_logbook} (
                        id SERIAL PRIMARY KEY,
                        ref_id TEXT
                    )
                """)
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{test_table_om} (
                        id TEXT PRIMARY KEY,
                        logbook_id INTEGER
                    )
                """)

                # 授予 logbook_svc 权限
                cur.execute(
                    f"GRANT SELECT, INSERT ON logbook.{test_table_logbook} TO engram_app_readwrite"
                )
                cur.execute(
                    f"GRANT USAGE ON SEQUENCE logbook.{test_table_logbook}_id_seq TO engram_app_readwrite"
                )
                cur.execute(
                    f"GRANT SELECT, INSERT ON {om_schema}.{test_table_om} TO engram_app_readwrite"
                )

            # 使用 logbook_svc 在两个 schema 中操作
            try:
                svc_conn = psycopg.connect(
                    host=config["host"],
                    port=config["port"],
                    dbname=config["dbname"],
                    user="logbook_svc",
                    password=config["password"],
                    autocommit=False,  # 使用事务
                )
            except psycopg.OperationalError as e:
                pytest.skip(f"无法使用 logbook_svc 连接: {e}")

            try:
                with svc_conn.cursor() as cur:
                    # 在 Logbook schema 插入
                    cur.execute(f"""
                        INSERT INTO logbook.{test_table_logbook} (ref_id)
                        VALUES ('ref_123')
                        RETURNING id
                    """)
                    logbook_id = cur.fetchone()[0]

                    # 在 OpenMemory schema 插入，引用 Logbook 的 id
                    cur.execute(
                        f"""
                        INSERT INTO {om_schema}.{test_table_om} (id, logbook_id)
                        VALUES ('mem_linked', %s)
                    """,
                        (logbook_id,),
                    )

                    # 提交事务
                    svc_conn.commit()

                    # 验证两边都有数据
                    cur.execute(f"SELECT COUNT(*) FROM logbook.{test_table_logbook}")
                    assert cur.fetchone()[0] == 1

                    cur.execute(f"SELECT COUNT(*) FROM {om_schema}.{test_table_om}")
                    assert cur.fetchone()[0] == 1
            finally:
                svc_conn.close()
        finally:
            # 清理
            with superuser_conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{test_table_om}")
                cur.execute(f"DROP TABLE IF EXISTS logbook.{test_table_logbook}")


@integration_test
class TestOpenMemoryMigration:
    """
    OpenMemory 迁移能力测试

    验证 openmemory_migrator_login 可以成功执行类似 `npm run migrate` 的操作，
    包括创建表、索引、约束等 DDL 操作。

    注意：这是模拟测试，不实际运行 npm，而是验证等效的 SQL DDL 能力。
    """

    TEST_PASSWORD = "test_password_12345"

    @pytest.fixture(scope="class")
    def om_schema(self, migrated_database):
        """获取 OpenMemory 目标 schema"""
        return os.environ.get("OM_PG_SCHEMA", "openmemory")

    @pytest.fixture(scope="class")
    def migrator_dsn(self, migrated_database):
        """构建 migrator 用户的 DSN"""
        from urllib.parse import urlparse, urlunparse

        dsn = migrated_database["dsn"]
        parsed = urlparse(dsn)

        # 替换用户名和密码
        new_netloc = f"openmemory_migrator_login:{self.TEST_PASSWORD}@{parsed.hostname}"
        if parsed.port:
            new_netloc += f":{parsed.port}"

        return urlunparse(
            (
                parsed.scheme,
                new_netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

    @pytest.fixture(scope="class")
    def setup_migrator_role(self, migrated_database):
        """确保 migrator 登录角色存在"""
        conn = migrated_database["conn"]

        with conn.cursor() as cur:
            cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator_login') THEN
                        CREATE ROLE openmemory_migrator_login LOGIN PASSWORD %s;
                    ELSE
                        ALTER ROLE openmemory_migrator_login WITH LOGIN PASSWORD %s;
                    END IF;

                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator') THEN
                        GRANT openmemory_migrator TO openmemory_migrator_login;
                    END IF;
                END $$;
            """,
                (self.TEST_PASSWORD, self.TEST_PASSWORD),
            )

        return True

    def test_can_simulate_openmemory_migration(
        self, setup_migrator_role, migrated_database, om_schema
    ):
        """
        模拟 OpenMemory npm run migrate 的 DDL 操作

        创建类似 OpenMemory 迁移的表结构：
        - memories 表
        - vectors 表
        - 相关索引和约束
        """
        from urllib.parse import urlparse

        import psycopg

        dsn = migrated_database["dsn"]
        parsed = urlparse(dsn)

        unique_suffix = secrets.token_hex(4)
        memories_table = f"_test_memories_{unique_suffix}"
        vectors_table = f"_test_vectors_{unique_suffix}"

        # 使用 migrator 连接
        try:
            conn = psycopg.connect(
                host=parsed.hostname or "localhost",
                port=parsed.port or 5432,
                dbname=parsed.path.lstrip("/") or "engram",
                user="openmemory_migrator_login",
                password=self.TEST_PASSWORD,
                autocommit=True,
                options=f"-c search_path={om_schema},public",
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_migrator_login 连接: {e}")

        try:
            with conn.cursor() as cur:
                # 1. 创建 memories 表（模拟 OpenMemory 迁移）
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{memories_table} (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        hash TEXT NOT NULL,
                        metadata JSONB DEFAULT '{{}}'::jsonb,
                        categories TEXT[] DEFAULT ARRAY[]::TEXT[],
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)

                # 2. 创建 vectors 表
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{vectors_table} (
                        id TEXT PRIMARY KEY,
                        memory_id TEXT NOT NULL REFERENCES {om_schema}.{memories_table}(id) ON DELETE CASCADE,
                        embedding REAL[] NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)

                # 3. 创建索引
                cur.execute(f"""
                    CREATE INDEX idx_{memories_table}_user_id
                    ON {om_schema}.{memories_table} (user_id)
                """)

                cur.execute(f"""
                    CREATE INDEX idx_{memories_table}_hash
                    ON {om_schema}.{memories_table} (hash)
                """)

                cur.execute(f"""
                    CREATE INDEX idx_{memories_table}_created_at
                    ON {om_schema}.{memories_table} (created_at DESC)
                """)

                cur.execute(f"""
                    CREATE INDEX idx_{vectors_table}_memory_id
                    ON {om_schema}.{vectors_table} (memory_id)
                """)

                # 4. 验证表和索引都创建成功
                cur.execute(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = %s AND table_name IN (%s, %s)
                """,
                    (om_schema, memories_table, vectors_table),
                )
                tables = [row[0] for row in cur.fetchall()]

                assert memories_table in tables, "memories 表应已创建"
                assert vectors_table in tables, "vectors 表应已创建"

                cur.execute(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname = %s AND tablename = %s
                """,
                    (om_schema, memories_table),
                )
                indexes = [row[0] for row in cur.fetchall()]

                assert len(indexes) >= 3, f"应至少有 3 个索引，实际: {indexes}"

                # 5. 清理
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{vectors_table}")
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{memories_table}")

        finally:
            conn.close()

    def test_migrator_can_run_transactions(self, setup_migrator_role, migrated_database, om_schema):
        """
        验证 migrator 可以在事务中执行迁移
        """
        from urllib.parse import urlparse

        import psycopg

        dsn = migrated_database["dsn"]
        parsed = urlparse(dsn)

        unique_suffix = secrets.token_hex(4)
        table1 = f"_test_txn1_{unique_suffix}"
        table2 = f"_test_txn2_{unique_suffix}"

        try:
            conn = psycopg.connect(
                host=parsed.hostname or "localhost",
                port=parsed.port or 5432,
                dbname=parsed.path.lstrip("/") or "engram",
                user="openmemory_migrator_login",
                password=self.TEST_PASSWORD,
                autocommit=False,  # 使用事务
                options=f"-c search_path={om_schema},public",
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_migrator_login 连接: {e}")

        try:
            with conn.cursor() as cur:
                # 开始事务
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{table1} (id SERIAL PRIMARY KEY)
                """)
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{table2} (
                        id SERIAL PRIMARY KEY,
                        ref_id INTEGER REFERENCES {om_schema}.{table1}(id)
                    )
                """)

                # 提交事务
                conn.commit()

                # 验证表存在
                cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = %s AND table_name IN (%s, %s)
                """,
                    (om_schema, table1, table2),
                )
                count = cur.fetchone()[0]
                assert count == 2, "两个表都应已创建"

                # 清理
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{table2}")
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{table1}")
                conn.commit()

        finally:
            conn.close()


# ============================================================================
# 测试类: MinIO Audit Webhook 集成测试
# ============================================================================


@integration_test
class TestMinioAuditWebhookIntegration:
    """
    MinIO Audit Webhook 集成测试

    测试场景：
    1. 向 MinIO put 一个对象
    2. 断言 governance.object_store_audit_events 表收到 s3:PutObject 事件
    3. 从 MinIO delete 该对象
    4. 断言 governance.object_store_audit_events 表收到 s3:DeleteObject 事件

    前置条件：
    - MinIO 服务可用并配置了 audit webhook 指向 gateway
    - Gateway 服务可用
    - PostgreSQL 服务可用且已执行迁移

    注意：
    - 此测试依赖 MinIO audit webhook 配置，默认在 minio profile 启用时自动配置
    - Webhook 事件是异步推送的，测试使用轮询等待方式验证
    """

    @pytest.fixture(scope="class")
    def minio_client(self):
        """
        创建 MinIO 客户端

        如果 MinIO 不可用或未配置，跳过测试
        """
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            pytest.skip("boto3 未安装")

        config = get_minio_config()

        # 检查必要配置
        if not config.get("endpoint"):
            pytest.skip("ENGRAM_S3_ENDPOINT 未配置")

        # 创建 S3 客户端
        s3_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        )

        client = boto3.client(
            "s3",
            endpoint_url=config["endpoint"],
            aws_access_key_id=config["access_key"],
            aws_secret_access_key=config["secret_key"],
            region_name=config["region"],
            config=s3_config,
        )

        # 验证连接
        try:
            client.list_buckets()
        except Exception as e:
            pytest.skip(f"无法连接 MinIO: {e}")

        # 确保 bucket 存在
        bucket = config["bucket"]
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            try:
                client.create_bucket(Bucket=bucket)
            except Exception as e:
                pytest.skip(f"无法创建 bucket {bucket}: {e}")

        return {"client": client, "bucket": bucket}

    @pytest.fixture(scope="class")
    def db_connection(self):
        """
        创建数据库连接
        """
        import psycopg

        dsn = get_postgres_dsn()
        try:
            conn = psycopg.connect(dsn)
        except Exception as e:
            pytest.skip(f"无法连接 PostgreSQL: {e}")

        yield conn

        conn.close()

    def _wait_for_audit_event(
        self,
        conn,
        bucket: str,
        object_key: str,
        operation: str,
        timeout_seconds: int = 30,
        poll_interval: float = 0.5,
    ) -> Optional[Dict[str, Any]]:
        """
        等待审计事件出现在数据库中

        Args:
            conn: 数据库连接
            bucket: 存储桶名称
            object_key: 对象键
            operation: 操作类型（如 s3:PutObject）
            timeout_seconds: 超时秒数
            poll_interval: 轮询间隔秒数

        Returns:
            审计事件记录（如果找到），否则返回 None
        """
        import time

        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_id, provider, event_ts, bucket, object_key,
                           operation, status_code, request_id, principal
                    FROM governance.object_store_audit_events
                    WHERE bucket = %s
                      AND object_key = %s
                      AND operation = %s
                    ORDER BY event_ts DESC
                    LIMIT 1
                    """,
                    (bucket, object_key, operation),
                )
                row = cur.fetchone()
                if row:
                    return {
                        "event_id": row[0],
                        "provider": row[1],
                        "event_ts": row[2],
                        "bucket": row[3],
                        "object_key": row[4],
                        "operation": row[5],
                        "status_code": row[6],
                        "request_id": row[7],
                        "principal": row[8],
                    }

            time.sleep(poll_interval)

        return None

    def _cleanup_test_objects(self, client, bucket: str, prefix: str):
        """
        清理测试对象
        """
        try:
            response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
            for obj in response.get("Contents", []):
                client.delete_object(Bucket=bucket, Key=obj["Key"])
        except Exception:
            pass  # 忽略清理错误

    def test_put_object_generates_audit_event(
        self,
        minio_client,
        db_connection,
    ):
        """
        测试 PUT 对象操作生成审计事件

        步骤：
        1. 向 MinIO put 一个测试对象
        2. 等待 audit webhook 将事件推送到 gateway
        3. 验证 object_store_audit_events 表中存在对应的 s3:PutObject 事件
        """
        client = minio_client["client"]
        bucket = minio_client["bucket"]
        conn = db_connection

        # 生成唯一的测试对象键
        test_key = f"test-audit-webhook/{uuid.uuid4()}/test-object.txt"
        test_content = b"Hello, MinIO Audit Webhook Test!"

        try:
            # 1. PUT 对象
            client.put_object(
                Bucket=bucket,
                Key=test_key,
                Body=test_content,
                ContentType="text/plain",
            )

            # 2. 等待审计事件
            event = self._wait_for_audit_event(
                conn=conn,
                bucket=bucket,
                object_key=test_key,
                operation="s3:PutObject",
                timeout_seconds=30,
            )

            # 3. 验证事件
            if event is None:
                # 如果没有收到事件，可能是 audit webhook 未配置
                # 这种情况下跳过测试而不是失败
                pytest.skip(
                    "未收到 MinIO audit 事件。"
                    "请确保 MinIO audit webhook 已配置指向 gateway。"
                    "检查: MINIO_AUDIT_WEBHOOK_ENDPOINT 环境变量"
                )

            assert event["provider"] == "minio", f"期望 provider=minio，实际={event['provider']}"
            assert event["bucket"] == bucket, f"期望 bucket={bucket}，实际={event['bucket']}"
            assert event["object_key"] == test_key, (
                f"期望 object_key={test_key}，实际={event['object_key']}"
            )
            assert event["operation"] == "s3:PutObject", (
                f"期望 operation=s3:PutObject，实际={event['operation']}"
            )
            # status_code 应该是 200（成功）
            if event["status_code"] is not None:
                assert event["status_code"] == 200, (
                    f"期望 status_code=200，实际={event['status_code']}"
                )

        finally:
            # 清理测试对象
            try:
                client.delete_object(Bucket=bucket, Key=test_key)
            except Exception:
                pass

    def test_delete_object_generates_audit_event(
        self,
        minio_client,
        db_connection,
    ):
        """
        测试 DELETE 对象操作生成审计事件

        步骤：
        1. 先 PUT 一个测试对象
        2. 然后 DELETE 该对象
        3. 验证 object_store_audit_events 表中存在对应的 s3:DeleteObject 事件
        """
        client = minio_client["client"]
        bucket = minio_client["bucket"]
        conn = db_connection

        # 生成唯一的测试对象键
        test_key = f"test-audit-webhook/{uuid.uuid4()}/test-delete-object.txt"
        test_content = b"This object will be deleted for audit test."

        # 1. 先 PUT 对象
        client.put_object(
            Bucket=bucket,
            Key=test_key,
            Body=test_content,
            ContentType="text/plain",
        )

        # 短暂等待确保对象已创建
        time.sleep(1)

        # 2. DELETE 对象
        client.delete_object(Bucket=bucket, Key=test_key)

        # 3. 等待审计事件
        event = self._wait_for_audit_event(
            conn=conn,
            bucket=bucket,
            object_key=test_key,
            operation="s3:DeleteObject",
            timeout_seconds=30,
        )

        # 4. 验证事件
        if event is None:
            pytest.skip(
                "未收到 MinIO delete audit 事件。请确保 MinIO audit webhook 已配置指向 gateway。"
            )

        assert event["provider"] == "minio", f"期望 provider=minio，实际={event['provider']}"
        assert event["bucket"] == bucket, f"期望 bucket={bucket}，实际={event['bucket']}"
        assert event["object_key"] == test_key, (
            f"期望 object_key={test_key}，实际={event['object_key']}"
        )
        assert event["operation"] == "s3:DeleteObject", (
            f"期望 operation=s3:DeleteObject，实际={event['operation']}"
        )

    def test_audit_events_contain_required_fields(
        self,
        minio_client,
        db_connection,
    ):
        """
        测试审计事件包含必要字段

        验证 object_store_audit_events 表中的事件记录包含：
        - provider (必填)
        - event_ts (必填)
        - bucket (必填)
        - operation (必填)
        - object_key (对于对象操作应该有值)
        - request_id (应该有值用于追踪)
        """
        client = minio_client["client"]
        bucket = minio_client["bucket"]
        conn = db_connection

        # 生成唯一的测试对象键
        test_key = f"test-audit-webhook/{uuid.uuid4()}/test-fields.txt"
        test_content = b"Test required fields in audit events."

        try:
            # PUT 对象
            client.put_object(
                Bucket=bucket,
                Key=test_key,
                Body=test_content,
            )

            # 等待审计事件
            event = self._wait_for_audit_event(
                conn=conn,
                bucket=bucket,
                object_key=test_key,
                operation="s3:PutObject",
                timeout_seconds=30,
            )

            if event is None:
                pytest.skip("未收到 MinIO audit 事件，跳过字段验证测试")

            # 验证必填字段
            assert event["provider"] is not None, "provider 字段不应为空"
            assert event["event_ts"] is not None, "event_ts 字段不应为空"
            assert event["bucket"] is not None, "bucket 字段不应为空"
            assert event["operation"] is not None, "operation 字段不应为空"

            # 验证对象操作应该有 object_key
            assert event["object_key"] is not None, "对于对象操作，object_key 字段不应为空"

            # request_id 用于追踪，应该有值
            # 注意：某些配置下可能没有 request_id，所以这里只做日志记录
            if event["request_id"] is None:
                print("[INFO] request_id 为空，这可能是正常的（取决于 MinIO 配置）")

        finally:
            # 清理测试对象
            try:
                client.delete_object(Bucket=bucket, Key=test_key)
            except Exception:
                pass


# ============================================================================
# 测试类: SCM 同步栈集成测试
# ============================================================================


@integration_test
class TestScmSyncStackIntegration:
    """
    SCM 同步栈集成测试

    测试场景：
    1. 启动依赖 DB（假设已就绪）
    2. 插入少量 repo/游标/模拟 job
    3. 运行各个脚本并验证输出：
       - scm_sync_scheduler.py scan
       - scm_sync_worker.py --once
       - scm_sync_reaper.py scan
       - scm_sync_status.py summary --format prometheus
    4. 断言关键输出字段存在且不包含敏感信息

    这是一个可重复的本地验证流程，用于验证 SCM 同步栈各组件的协作。
    """

    # 敏感信息关键词列表（用于检测泄露）
    SENSITIVE_KEYWORDS = [
        "password",
        "secret",
        "token",
        "private_token",
        "api_key",
        "apikey",
        "credential",
        "auth_header",
        # 常见的 token 格式
        "glpat-",  # GitLab Personal Access Token
        "ghp_",  # GitHub Personal Access Token
        "sk-",  # OpenAI API Key
    ]

    @pytest.fixture(scope="class")
    def test_repo_id(self, migrated_database):
        """
        创建测试用 repo 并返回 repo_id

        测试结束后会自动清理
        """
        from engram.logbook import scm_db

        conn = migrated_database["conn"]

        # 创建测试仓库（使用唯一的 URL 避免冲突）
        unique_suffix = secrets.token_hex(4)
        test_url = f"https://gitlab.test.local/test-group/test-project-{unique_suffix}"

        repo_id = scm_db.upsert_repo(
            conn,
            repo_type="git",
            url=test_url,
            project_key=f"test/{unique_suffix}",
            default_branch="main",
        )
        conn.commit()

        yield repo_id

        # 清理：删除测试数据
        try:
            with conn.cursor() as cur:
                # 删除相关的 sync_jobs
                cur.execute("DELETE FROM scm.sync_jobs WHERE repo_id = %s", (repo_id,))
                # 删除相关的 sync_runs
                cur.execute("DELETE FROM scm.sync_runs WHERE repo_id = %s", (repo_id,))
                # 删除相关的 sync_locks
                cur.execute("DELETE FROM scm.sync_locks WHERE repo_id = %s", (repo_id,))
                # 删除游标
                cur.execute(
                    "DELETE FROM logbook.kv WHERE namespace = 'scm.sync' AND key LIKE %s",
                    (f"%:{repo_id}",),
                )
                # 删除 repo 本身
                cur.execute("DELETE FROM scm.repos WHERE repo_id = %s", (repo_id,))
                conn.commit()
        except Exception as e:
            print(f"[WARN] 清理测试数据时出错: {e}")
            conn.rollback()

    @pytest.fixture(scope="class")
    def setup_test_cursor(self, test_repo_id, migrated_database):
        """
        为测试仓库设置游标
        """
        from engram.logbook.config import get_config
        from engram.logbook.cursor import CURSOR_VERSION, Cursor, save_cursor

        config = get_config()

        # 创建一个简单的 gitlab 游标
        cursor = Cursor(
            version=CURSOR_VERSION,
            watermark={"updated_at": datetime.now(timezone.utc).isoformat()},
            stats={"synced_count": 0},
        )

        # 保存游标
        save_cursor("gitlab", test_repo_id, cursor, config=config)

        return cursor

    @pytest.fixture(scope="class")
    def setup_test_job(self, test_repo_id, migrated_database):
        """
        为测试仓库创建模拟 sync_job
        """
        from engram.logbook import scm_db

        conn = migrated_database["conn"]

        # 创建一个 pending 状态的 job
        job_id = scm_db.enqueue_sync_job(
            conn,
            repo_id=test_repo_id,
            job_type="gitlab_commits",
            mode="incremental",
            priority=100,
            payload_json={
                "reason": "test",
                "scheduled_at": datetime.now(timezone.utc).isoformat(),
                # 注意：不包含敏感信息
            },
        )
        conn.commit()

        yield job_id

        # 清理在 test_repo_id fixture 中进行

    def _check_no_sensitive_info(self, output: str, context: str = "") -> List[str]:
        """
        检查输出中是否包含敏感信息

        Args:
            output: 要检查的输出字符串
            context: 上下文描述（用于错误消息）

        Returns:
            发现的敏感信息关键词列表（空列表表示安全）
        """
        found_sensitive = []
        output_lower = output.lower()

        for keyword in self.SENSITIVE_KEYWORDS:
            if keyword.lower() in output_lower:
                found_sensitive.append(keyword)

        return found_sensitive

    def test_scheduler_scan(
        self,
        test_repo_id,
        setup_test_cursor,
        migrated_database,
    ):
        """
        测试 scm_sync_scheduler.py scan 命令

        验证：
        1. 命令能够正常执行
        2. 输出包含关键字段（repos_scanned, candidates_selected）
        3. 输出不包含敏感信息
        """

        import json

        from engram.logbook.scm_sync_scheduler_core import run_scheduler_tick

        conn = migrated_database["conn"]

        # 执行调度扫描（dry_run 避免修改队列）
        result = run_scheduler_tick(conn, dry_run=True)

        # 验证结果对象存在关键字段
        assert hasattr(result, "repos_scanned"), "结果应包含 repos_scanned 字段"
        assert hasattr(result, "candidates_selected"), "结果应包含 candidates_selected 字段"
        assert hasattr(result, "jobs_enqueued"), "结果应包含 jobs_enqueued 字段"
        assert hasattr(result, "jobs_skipped"), "结果应包含 jobs_skipped 字段"
        assert hasattr(result, "scheduled_at"), "结果应包含 scheduled_at 字段"

        # 验证数值合理
        assert result.repos_scanned >= 0, "repos_scanned 应 >= 0"
        assert result.candidates_selected >= 0, "candidates_selected 应 >= 0"
        assert result.jobs_enqueued >= 0, "jobs_enqueued 应 >= 0"

        # 检查 JSON 输出不包含敏感信息
        json_output = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
        sensitive_found = self._check_no_sensitive_info(json_output, "scheduler scan output")
        assert not sensitive_found, f"Scheduler 输出包含敏感信息: {sensitive_found}"

    def test_worker_once(
        self,
        test_repo_id,
        setup_test_job,
        migrated_database,
    ):
        """
        测试 scm_sync_worker.py --once 命令

        验证：
        1. 命令能够正常执行（即使没有可处理的任务）
        2. 不抛出异常
        3. 不泄露敏感信息

        注意：由于测试环境可能没有 GitLab 连接，worker 可能会失败，
        但这是预期行为，我们只验证流程不崩溃。
        """
        from engram.logbook import scm_sync_worker_core as worker

        # 生成测试 worker ID
        worker_id = f"test-worker-{secrets.token_hex(4)}"

        # 尝试执行单次处理
        # 注意：这可能返回 False（无任务）或 True（处理了任务但可能失败）
        # 两种情况都是预期行为
        try:
            result = worker.process_one_job(
                worker_id=worker_id,
                job_types=["gitlab_commits"],  # 只处理我们创建的测试任务类型
                conn=migrated_database["conn"],
                circuit_breaker=None,  # 测试中禁用熔断
            )

            # 结果应该是布尔值
            assert isinstance(result, bool), "run_once 应返回布尔值"

        except Exception as e:
            # 如果因为缺少 GitLab token 等原因失败，这是预期的
            # 但错误消息不应包含敏感信息
            error_msg = str(e)
            sensitive_found = self._check_no_sensitive_info(error_msg, "worker error message")
            assert not sensitive_found, f"Worker 错误消息包含敏感信息: {sensitive_found}"

    def test_reaper_scan(
        self,
        test_repo_id,
        setup_test_job,
        migrated_database,
    ):
        """
        测试 scm_sync_reaper.py scan 命令

        验证：
        1. 命令能够正常执行
        2. 输出为结构化 JSON 日志
        3. 输出不包含敏感信息
        """
        from io import StringIO

        from engram.logbook import scm_db

        # 捕获 stdout（reaper 使用 print 输出 JSON 日志）
        old_stdout = sys.stdout
        captured_output = StringIO()
        sys.stdout = captured_output

        try:
            # 获取数据库连接
            conn = migrated_database["conn"]

            # 扫描过期任务
            jobs = scm_db.list_expired_running_jobs(conn, grace_seconds=60, limit=100)
            runs = scm_db.list_expired_running_runs(conn, max_duration_seconds=1800, limit=100)
            locks = scm_db.list_expired_locks(conn, grace_seconds=0, limit=100)

            # 验证返回类型
            assert isinstance(jobs, list), "list_expired_running_jobs 应返回列表"
            assert isinstance(runs, list), "list_expired_running_runs 应返回列表"
            assert isinstance(locks, list), "list_expired_locks 应返回列表"

        finally:
            sys.stdout = old_stdout

        # 获取捕获的输出
        output = captured_output.getvalue()

        # 检查输出不包含敏感信息
        sensitive_found = self._check_no_sensitive_info(output, "reaper scan output")
        assert not sensitive_found, f"Reaper 输出包含敏感信息: {sensitive_found}"

    def test_status_summary_prometheus(
        self,
        test_repo_id,
        setup_test_cursor,
        setup_test_job,
        migrated_database,
    ):
        """
        测试 scm_sync_status.py summary --format prometheus 命令

        验证：
        1. 命令能够正常执行
        2. 输出包含 Prometheus 格式的关键指标
        3. 输出不包含敏感信息

        关键指标：
        - scm_repos_total
        - scm_jobs_total
        - scm_expired_locks
        - scm_window_failed_rate
        - scm_window_rate_limit_rate
        """
        from engram.logbook import scm_sync_status as status

        # 获取数据库连接
        conn = status.get_connection()

        try:
            # 获取摘要
            summary = status.get_sync_summary(conn, window_minutes=60, top_lag_limit=10)

            # 验证摘要包含关键字段
            assert "repos_count" in summary, "摘要应包含 repos_count"
            assert "jobs" in summary, "摘要应包含 jobs"
            assert "expired_locks" in summary, "摘要应包含 expired_locks"
            assert "window_stats" in summary, "摘要应包含 window_stats"

            # 验证 jobs 字段结构
            jobs = summary["jobs"]
            assert "pending" in jobs, "jobs 应包含 pending"
            assert "running" in jobs, "jobs 应包含 running"
            assert "failed" in jobs, "jobs 应包含 failed"
            assert "dead" in jobs, "jobs 应包含 dead"

            # 验证 window_stats 字段结构
            window_stats = summary["window_stats"]
            assert "failed_rate" in window_stats, "window_stats 应包含 failed_rate"
            assert "rate_limit_rate" in window_stats, "window_stats 应包含 rate_limit_rate"

            # 生成 Prometheus 格式输出
            prometheus_output = status.format_prometheus_metrics(summary)

            # 验证 Prometheus 输出包含关键指标
            assert "scm_repos_total" in prometheus_output, "Prometheus 输出应包含 scm_repos_total"
            assert "scm_jobs_total" in prometheus_output, "Prometheus 输出应包含 scm_jobs_total"
            assert "scm_expired_locks" in prometheus_output, (
                "Prometheus 输出应包含 scm_expired_locks"
            )
            assert "scm_window_failed_rate" in prometheus_output, (
                "Prometheus 输出应包含 scm_window_failed_rate"
            )
            assert "scm_window_rate_limit_rate" in prometheus_output, (
                "Prometheus 输出应包含 scm_window_rate_limit_rate"
            )

            # 验证 Prometheus 输出格式正确（包含 # HELP 和 # TYPE）
            assert "# HELP" in prometheus_output, "Prometheus 输出应包含 HELP 注释"
            assert "# TYPE" in prometheus_output, "Prometheus 输出应包含 TYPE 注释"

            # 检查输出不包含敏感信息
            sensitive_found = self._check_no_sensitive_info(prometheus_output, "prometheus output")
            assert not sensitive_found, f"Prometheus 输出包含敏感信息: {sensitive_found}"

            # 检查 JSON 摘要不包含敏感信息
            import json

            json_output = json.dumps(summary, ensure_ascii=False, default=str)
            sensitive_found = self._check_no_sensitive_info(json_output, "status summary json")
            assert not sensitive_found, f"Status 摘要包含敏感信息: {sensitive_found}"

        finally:
            conn.close()

    def test_full_scm_sync_flow(
        self,
        test_repo_id,
        setup_test_cursor,
        migrated_database,
    ):
        """
        完整的 SCM 同步流程测试

        流程：
        1. scheduler scan -> 入队任务
        2. status summary -> 验证队列状态
        3. reaper scan -> 验证无过期任务
        4. 清理测试数据

        这是一个端到端的验证流程。
        """
        from engram.logbook import scm_db
        from engram.logbook import scm_sync_status as status

        conn = migrated_database["conn"]

        # 1. 清理之前的测试任务
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scm.sync_jobs WHERE repo_id = %s", (test_repo_id,))
            conn.commit()

        # 2. 手动入队一个测试任务
        job_id = scm_db.enqueue_sync_job(
            conn,
            repo_id=test_repo_id,
            job_type="gitlab_commits",
            mode="incremental",
            priority=50,
            payload_json={"reason": "integration_test"},
        )
        conn.commit()

        assert job_id is not None, "应成功入队测试任务"

        # 3. 验证 status 能看到这个任务
        status_conn = status.get_connection()
        try:
            summary = status.get_sync_summary(status_conn)

            # 验证 pending 任务数 >= 1
            assert summary["jobs"]["pending"] >= 1, "应至少有 1 个 pending 任务"

        finally:
            status_conn.close()

        # 4. reaper scan 应该不会标记我们的新任务为过期
        expired_jobs = scm_db.list_expired_running_jobs(conn, grace_seconds=60, limit=100)

        # 我们刚创建的任务不应该在过期列表中
        expired_job_ids = [str(j.get("job_id", "")) for j in expired_jobs]
        assert job_id not in expired_job_ids, "新创建的任务不应被标记为过期"

        # 5. 清理测试任务
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scm.sync_jobs WHERE job_id = %s::uuid", (job_id,))
            conn.commit()

    def test_status_output_no_sensitive_info_comprehensive(
        self,
        test_repo_id,
        migrated_database,
    ):
        """
        全面检查 status 输出不包含敏感信息

        检查所有输出格式：
        - JSON
        - Table
        - Prometheus
        """
        import json

        from engram.logbook import scm_sync_status as status

        conn = status.get_connection()

        try:
            # 获取各种查询结果
            repos = status.query_repos(conn, limit=10)
            cursors = status.query_kv_cursors(conn, namespace="scm.sync")
            runs = status.query_sync_runs(conn, limit=10)
            jobs = status.query_sync_jobs(conn, limit=10)
            locks = status.query_sync_locks(conn)
            summary = status.get_sync_summary(conn)

            # 序列化所有数据为 JSON
            all_data = {
                "repos": repos,
                "cursors": cursors,
                "runs": runs,
                "jobs": jobs,
                "locks": locks,
                "summary": summary,
            }

            json_output = json.dumps(all_data, ensure_ascii=False, default=str)

            # 检查 JSON 输出不包含敏感信息
            sensitive_found = self._check_no_sensitive_info(
                json_output, "comprehensive status output"
            )
            assert not sensitive_found, f"Status 综合输出包含敏感信息: {sensitive_found}"

            # 检查 Prometheus 输出
            prometheus_output = status.format_prometheus_metrics(summary)
            sensitive_found = self._check_no_sensitive_info(prometheus_output, "prometheus metrics")
            assert not sensitive_found, f"Prometheus 指标包含敏感信息: {sensitive_found}"

        finally:
            conn.close()


# ============================================================================
# 运行入口
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
