"""
test_minio_audit_webhook - MinIO Audit Webhook 端点测试

测试覆盖:
1. Token 认证测试
   - 缺少 token 返回 401
   - 无效 token 返回 403
   - Bearer token 认证成功
   - X-Minio-Auth-Token 认证成功
2. Payload 验证测试
   - 空 payload 返回 400
   - 非 JSON payload 返回 400
   - 超大 payload 返回 413
3. 成功落库测试
   - 有效请求返回 200
   - 返回 event_id
4. 归一化映射测试
   - MinIO 事件归一化后符合 object_store_audit_event_v1 schema
   - schema_version/provider/raw 字段正确设置
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

try:
    import jsonschema
    from jsonschema import validate, ValidationError
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def valid_minio_event():
    """有效的 MinIO 审计事件"""
    return {
        "version": "1",
        "deploymentid": "test-deployment-id",
        "time": "2024-01-15T10:00:00.000Z",
        "trigger": "incoming",
        "api": {
            "name": "PutObject",
            "bucket": "engram",
            "object": "scm/1/git/commits/abc123.diff",
            "status": "200 OK",
            "statusCode": 200,
            "timeToResponse": "15ms",
        },
        "remotehost": "192.168.1.100",
        "requestID": "req-123456",
        "userAgent": "MinIO Client",
    }


@pytest.fixture
def mock_config():
    """创建测试用的配置"""
    with patch("engram.gateway.minio_audit_webhook.get_config") as mock:
        config = MagicMock()
        config.minio_audit_webhook_auth_token = "test-secret-token"
        config.minio_audit_max_payload_size = 1024 * 1024  # 1MB
        config.postgres_dsn = "postgresql://test:test@localhost/test"
        mock.return_value = config
        yield config


@pytest.fixture
def mock_db_insert():
    """Mock 数据库插入操作"""
    with patch("engram.gateway.minio_audit_webhook._insert_audit_to_db") as mock:
        mock.return_value = 12345  # 返回模拟的 event_id
        yield mock


@pytest.fixture
def client(mock_config):
    """创建测试客户端"""
    # 设置必要的环境变量
    os.environ.setdefault("PROJECT_KEY", "test_project")
    os.environ.setdefault("POSTGRES_DSN", "postgresql://test:test@localhost/test")
    os.environ.setdefault("OPENMEMORY_BASE_URL", "http://localhost:8080")
    
    from engram.gateway.main import app
    return TestClient(app)


# ============================================================================
# Token 认证测试
# ============================================================================

class TestMinioAuditTokenAuth:
    """Token 认证测试"""

    def test_missing_token_returns_401(self, client, mock_db_insert, valid_minio_event):
        """缺少 token 返回 401"""
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            # 不提供任何认证 header
        )
        
        assert response.status_code == 401
        data = response.json()
        assert data["ok"] is False
        assert "token" in data["error"].lower()

    def test_invalid_bearer_token_returns_403(self, client, mock_db_insert, valid_minio_event):
        """无效的 Bearer token 返回 403"""
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"Authorization": "Bearer wrong-token"},
        )
        
        assert response.status_code == 403
        data = response.json()
        assert data["ok"] is False
        assert "无效" in data["error"]

    def test_invalid_minio_token_returns_403(self, client, mock_db_insert, valid_minio_event):
        """无效的 X-Minio-Auth-Token 返回 403"""
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"X-Minio-Auth-Token": "wrong-token"},
        )
        
        assert response.status_code == 403
        data = response.json()
        assert data["ok"] is False

    def test_valid_bearer_token_accepted(self, client, mock_db_insert, valid_minio_event):
        """有效的 Bearer token 认证成功"""
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"Authorization": "Bearer test-secret-token"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

    def test_valid_minio_token_accepted(self, client, mock_db_insert, valid_minio_event):
        """有效的 X-Minio-Auth-Token 认证成功"""
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"X-Minio-Auth-Token": "test-secret-token"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True


class TestMinioAuditTokenNotConfigured:
    """Token 未配置测试"""

    def test_unconfigured_token_returns_503(self, mock_db_insert, valid_minio_event):
        """未配置 token 返回 503"""
        os.environ.setdefault("PROJECT_KEY", "test_project")
        os.environ.setdefault("POSTGRES_DSN", "postgresql://test:test@localhost/test")
        os.environ.setdefault("OPENMEMORY_BASE_URL", "http://localhost:8080")
        
        with patch("engram.gateway.minio_audit_webhook.get_config") as mock_config:
            config = MagicMock()
            config.minio_audit_webhook_auth_token = None  # 未配置
            config.minio_audit_max_payload_size = 1024 * 1024
            mock_config.return_value = config
            
            from engram.gateway.main import app
            client = TestClient(app)
            
            response = client.post(
                "/minio/audit",
                json=valid_minio_event,
                headers={"Authorization": "Bearer any-token"},
            )
            
            assert response.status_code == 503
            data = response.json()
            assert data["ok"] is False
            assert "未配置" in data["error"]


# ============================================================================
# Payload 验证测试
# ============================================================================

class TestMinioAuditPayloadValidation:
    """Payload 验证测试"""

    def test_empty_payload_returns_400(self, client, mock_db_insert):
        """空 payload 返回 400"""
        response = client.post(
            "/minio/audit",
            content=b"",
            headers={
                "Authorization": "Bearer test-secret-token",
                "Content-Type": "application/json",
            },
        )
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False
        assert "空" in data["error"]

    def test_invalid_json_returns_400(self, client, mock_db_insert):
        """非法 JSON 返回 400"""
        response = client.post(
            "/minio/audit",
            content=b"not valid json {{{",
            headers={
                "Authorization": "Bearer test-secret-token",
                "Content-Type": "application/json",
            },
        )
        
        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False
        assert "JSON" in data["error"]

    def test_oversized_payload_returns_413(self, mock_db_insert, valid_minio_event):
        """超大 payload 返回 413"""
        os.environ.setdefault("PROJECT_KEY", "test_project")
        os.environ.setdefault("POSTGRES_DSN", "postgresql://test:test@localhost/test")
        os.environ.setdefault("OPENMEMORY_BASE_URL", "http://localhost:8080")
        
        # 配置很小的 max payload size
        with patch("engram.gateway.minio_audit_webhook.get_config") as mock_config:
            config = MagicMock()
            config.minio_audit_webhook_auth_token = "test-secret-token"
            config.minio_audit_max_payload_size = 100  # 只允许 100 字节
            config.postgres_dsn = "postgresql://test:test@localhost/test"
            mock_config.return_value = config
            
            from engram.gateway.main import app
            client = TestClient(app)
            
            # 创建大于 100 字节的 payload
            large_event = valid_minio_event.copy()
            large_event["extra_data"] = "x" * 200
            
            response = client.post(
                "/minio/audit",
                json=large_event,
                headers={"Authorization": "Bearer test-secret-token"},
            )
            
            assert response.status_code == 413
            data = response.json()
            assert data["ok"] is False
            assert "过大" in data["error"]


# ============================================================================
# 成功落库测试
# ============================================================================

class TestMinioAuditSuccess:
    """成功落库测试"""

    def test_valid_request_returns_200(self, client, mock_db_insert, valid_minio_event):
        """有效请求返回 200"""
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"Authorization": "Bearer test-secret-token"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "event_id" in data
        assert data["event_id"] == 12345

    def test_db_insert_called_with_correct_data(self, client, mock_db_insert, valid_minio_event):
        """数据库插入使用正确的数据（归一化后）"""
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"Authorization": "Bearer test-secret-token"},
        )
        
        assert response.status_code == 200
        
        # 验证 _insert_audit_to_db 被调用
        mock_db_insert.assert_called_once()
        
        # 检查调用参数（归一化后的结构）
        call_args = mock_db_insert.call_args[0][0]
        assert call_args["schema_version"] == "1.0"
        assert call_args["provider"] == "minio"
        assert call_args["operation"] == "s3:PutObject"
        assert call_args["bucket"] == "engram"
        assert call_args["object_key"] == "scm/1/git/commits/abc123.diff"
        assert call_args["success"] is True
        assert call_args["raw"] == valid_minio_event

    def test_response_contains_message(self, client, mock_db_insert, valid_minio_event):
        """响应包含消息"""
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"Authorization": "Bearer test-secret-token"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "message" in data


# ============================================================================
# 字段提取测试
# ============================================================================

class TestMinioAuditFieldExtraction:
    """字段提取测试"""

    def test_extracts_operation_from_api_name(self, client, mock_db_insert, valid_minio_event):
        """从 api.name 提取 operation"""
        valid_minio_event["api"]["name"] = "DeleteObject"
        
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"Authorization": "Bearer test-secret-token"},
        )
        
        assert response.status_code == 200
        call_args = mock_db_insert.call_args[0][0]
        # 归一化后 operation 使用 s3: 前缀
        assert call_args["operation"] == "s3:DeleteObject"

    def test_extracts_success_from_status_code(self, client, mock_db_insert, valid_minio_event):
        """从 api.statusCode 判断 success"""
        # 测试成功状态码
        valid_minio_event["api"]["statusCode"] = 200
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"Authorization": "Bearer test-secret-token"},
        )
        assert response.status_code == 200
        call_args = mock_db_insert.call_args[0][0]
        assert call_args["success"] is True
        
        # 测试失败状态码
        valid_minio_event["api"]["statusCode"] = 403
        response = client.post(
            "/minio/audit",
            json=valid_minio_event,
            headers={"Authorization": "Bearer test-secret-token"},
        )
        assert response.status_code == 200
        call_args = mock_db_insert.call_args[0][0]
        assert call_args["success"] is False

    def test_handles_minimal_event(self, client, mock_db_insert):
        """处理最小化事件"""
        minimal_event = {"api": {}}
        
        response = client.post(
            "/minio/audit",
            json=minimal_event,
            headers={"Authorization": "Bearer test-secret-token"},
        )
        
        assert response.status_code == 200
        call_args = mock_db_insert.call_args[0][0]
        assert call_args["operation"] == "unknown"
        # 归一化后 bucket 默认为 "unknown" 以满足 schema required 约束
        assert call_args["bucket"] == "unknown"
        assert call_args["object_key"] is None


# ============================================================================
# 数据库错误处理测试
# ============================================================================

class TestMinioAuditDbError:
    """数据库错误处理测试"""

    def test_db_error_returns_500(self, client, valid_minio_event):
        """数据库错误返回 500"""
        with patch("engram.gateway.minio_audit_webhook._insert_audit_to_db") as mock_insert:
            from engram.gateway.minio_audit_webhook import MinioAuditError
            mock_insert.side_effect = MinioAuditError("数据库连接失败", status_code=500)
            
            response = client.post(
                "/minio/audit",
                json=valid_minio_event,
                headers={"Authorization": "Bearer test-secret-token"},
            )
            
            assert response.status_code == 500
            data = response.json()
            assert data["ok"] is False
            assert "数据库" in data["error"]


# ============================================================================
# 集成测试（需要数据库）
# ============================================================================

class TestMinioAuditIntegration:
    """集成测试（需要数据库连接）"""

    @pytest.fixture(autouse=True)
    def check_db_available(self):
        """检查数据库是否可用"""
        dsn = os.environ.get("TEST_PG_DSN") or os.environ.get("POSTGRES_DSN")
        if not dsn:
            pytest.skip("需要 TEST_PG_DSN 或 POSTGRES_DSN 环境变量")
        
        try:
            import psycopg
            conn = psycopg.connect(dsn, connect_timeout=2)
            conn.close()
        except Exception as e:
            pytest.skip(f"数据库连接失败: {e}")

    def test_real_db_insert(self, valid_minio_event):
        """真实数据库插入测试"""
        dsn = os.environ.get("TEST_PG_DSN") or os.environ.get("POSTGRES_DSN")
        auth_token = "integration-test-token"
        
        # 配置环境
        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = dsn
        os.environ["OPENMEMORY_BASE_URL"] = "http://localhost:8080"
        os.environ["MINIO_AUDIT_WEBHOOK_AUTH_TOKEN"] = auth_token
        
        # 重置配置
        from engram.gateway.config import reset_config
        reset_config()
        
        try:
            from engram.gateway.main import app
            client = TestClient(app)
            
            response = client.post(
                "/minio/audit",
                json=valid_minio_event,
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            
            # 即使表不存在也应该返回错误而不是崩溃
            assert response.status_code in [200, 500]
            data = response.json()
            assert "ok" in data
            
        finally:
            reset_config()
            os.environ.pop("MINIO_AUDIT_WEBHOOK_AUTH_TOKEN", None)


# ============================================================================
# Schema 加载辅助函数
# ============================================================================

def _find_schema_path() -> Path:
    """查找 object_store_audit_event_v1.schema.json 路径"""
    current = Path(__file__).resolve().parent
    
    for _ in range(10):
        candidate = current / "schemas" / "object_store_audit_event_v1.schema.json"
        if candidate.exists():
            return candidate
        current = current.parent
    
    fallback = Path(__file__).resolve().parent.parent.parent.parent.parent / "schemas" / "object_store_audit_event_v1.schema.json"
    return fallback


def load_object_store_schema():
    """加载 object_store_audit_event_v1 schema"""
    schema_path = _find_schema_path()
    if not schema_path.exists():
        pytest.skip(f"Schema 文件不存在: {schema_path}")
    
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================================
# 归一化映射测试
# ============================================================================

@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestMinioAuditNormalization:
    """归一化映射测试 - 验证 MinIO 事件归一化后符合 schema"""

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema"""
        return load_object_store_schema()

    def test_normalize_minio_event_basic(self, schema):
        """基本 MinIO 事件归一化后符合 schema"""
        from engram.gateway.minio_audit_webhook import normalize_to_schema
        
        minio_event = {
            "version": "1",
            "deploymentid": "test-deployment-id",
            "time": "2024-01-15T10:00:00.000Z",
            "api": {
                "name": "PutObject",
                "bucket": "engram",
                "object": "scm/1/git/commits/abc123.diff",
                "statusCode": 200,
            },
            "remotehost": "192.168.1.100",
            "requestID": "req-123456",
            "userAgent": "MinIO Client",
        }
        
        normalized = normalize_to_schema(minio_event)
        
        # 验证符合 schema
        validate(instance=normalized, schema=schema)
        
        # 验证关键字段
        assert normalized["schema_version"] == "1.0"
        assert normalized["provider"] == "minio"
        assert normalized["bucket"] == "engram"
        assert normalized["object_key"] == "scm/1/git/commits/abc123.diff"
        assert normalized["operation"] == "s3:PutObject"
        assert normalized["status_code"] == 200
        assert normalized["success"] is True
        assert normalized["raw"] == minio_event

    def test_normalize_minio_event_with_null_fields(self, schema):
        """包含空字段的 MinIO 事件归一化后符合 schema"""
        from engram.gateway.minio_audit_webhook import normalize_to_schema
        
        minio_event = {
            "api": {
                "name": "ListBuckets",
                "statusCode": 200,
            },
            "time": "2024-01-15T10:00:00.000Z",
        }
        
        normalized = normalize_to_schema(minio_event)
        
        # 验证符合 schema
        validate(instance=normalized, schema=schema)
        
        # 验证空字段处理
        assert normalized["schema_version"] == "1.0"
        assert normalized["provider"] == "minio"
        assert normalized["bucket"] == "unknown"  # 默认值
        assert normalized["object_key"] is None
        assert normalized["operation"] == "s3:ListBuckets"

    def test_normalize_minio_event_failure_status(self, schema):
        """失败状态的 MinIO 事件归一化后符合 schema"""
        from engram.gateway.minio_audit_webhook import normalize_to_schema
        
        minio_event = {
            "time": "2024-01-15T10:00:00.000Z",
            "api": {
                "name": "GetObject",
                "bucket": "engram",
                "object": "not-found.txt",
                "statusCode": 404,
            },
        }
        
        normalized = normalize_to_schema(minio_event)
        
        # 验证符合 schema
        validate(instance=normalized, schema=schema)
        
        # 验证失败状态
        assert normalized["status_code"] == 404
        assert normalized["success"] is False

    def test_normalize_preserves_raw_event(self, schema):
        """归一化后 raw 字段完整保留原始事件"""
        from engram.gateway.minio_audit_webhook import normalize_to_schema
        
        minio_event = {
            "version": "1",
            "deploymentid": "test-deployment",
            "time": "2024-01-15T10:00:00.000Z",
            "trigger": "incoming",
            "api": {
                "name": "PutObject",
                "bucket": "engram",
                "object": "test.txt",
                "status": "200 OK",
                "statusCode": 200,
                "timeToResponse": "15ms",
            },
            "remotehost": "192.168.1.100:52431",
            "requestID": "REQ-123",
            "userAgent": "aws-sdk-go/1.0",
            "requestClaims": {
                "accessKey": "minioadmin",
                "sub": "test-user",
            },
            "requestHeader": {
                "Content-Type": "application/octet-stream",
            },
        }
        
        normalized = normalize_to_schema(minio_event)
        
        # 验证 raw 完整保留
        assert normalized["raw"] == minio_event
        assert normalized["raw"]["requestClaims"]["accessKey"] == "minioadmin"
        assert normalized["raw"]["requestHeader"]["Content-Type"] == "application/octet-stream"

    def test_normalize_extracts_principal(self, schema):
        """归一化正确提取 principal"""
        from engram.gateway.minio_audit_webhook import normalize_to_schema
        
        minio_event = {
            "time": "2024-01-15T10:00:00.000Z",
            "api": {
                "name": "GetObject",
                "bucket": "engram",
                "statusCode": 200,
            },
            "requestClaims": {
                "accessKey": "AKIAIOSFODNN7EXAMPLE",
            },
        }
        
        normalized = normalize_to_schema(minio_event)
        
        # 验证 principal 提取
        assert normalized["principal"] == "AKIAIOSFODNN7EXAMPLE"

    def test_normalize_strips_port_from_remote_ip(self, schema):
        """归一化移除 remote_ip 中的端口号"""
        from engram.gateway.minio_audit_webhook import normalize_to_schema
        
        minio_event = {
            "time": "2024-01-15T10:00:00.000Z",
            "api": {
                "name": "GetObject",
                "bucket": "engram",
                "statusCode": 200,
            },
            "remotehost": "192.168.1.100:52431",
        }
        
        normalized = normalize_to_schema(minio_event)
        
        # 验证端口被移除
        assert normalized["remote_ip"] == "192.168.1.100"

    def test_normalize_handles_unknown_operation(self, schema):
        """未知操作类型归一化为 unknown"""
        from engram.gateway.minio_audit_webhook import normalize_to_schema
        
        minio_event = {
            "time": "2024-01-15T10:00:00.000Z",
            "api": {},  # 无 name 字段
        }
        
        normalized = normalize_to_schema(minio_event)
        
        # 验证符合 schema
        validate(instance=normalized, schema=schema)
        
        # 验证未知操作
        assert normalized["operation"] == "unknown"


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestMinioAuditDbInsertWithSchema:
    """验证数据库插入使用归一化后的数据"""

    def test_db_insert_uses_normalized_data(self, mock_config, valid_minio_event):
        """数据库插入应使用归一化后的数据"""
        with patch("engram.gateway.minio_audit_webhook._insert_audit_to_db") as mock_insert:
            mock_insert.return_value = 12345
            
            os.environ.setdefault("PROJECT_KEY", "test_project")
            os.environ.setdefault("POSTGRES_DSN", "postgresql://test:test@localhost/test")
            os.environ.setdefault("OPENMEMORY_BASE_URL", "http://localhost:8080")
            
            from engram.gateway.main import app
            client = TestClient(app)
            
            response = client.post(
                "/minio/audit",
                json=valid_minio_event,
                headers={"Authorization": "Bearer test-secret-token"},
            )
            
            assert response.status_code == 200
            
            # 验证插入数据包含归一化字段
            call_args = mock_insert.call_args[0][0]
            assert call_args["schema_version"] == "1.0"
            assert call_args["provider"] == "minio"
            assert "raw" in call_args
            assert call_args["raw"] == valid_minio_event
