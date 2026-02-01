"""
测试 evidence_upload 工具

测试覆盖：
1. 正常上传文本内容
2. 大小限制校验
3. 内容类型校验
4. 返回 evidence(v2) 对象格式
"""

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from engram.gateway.evidence_store import (
    ALLOWED_CONTENT_TYPES,
    DEFAULT_MAX_SIZE_BYTES,
    EvidenceContentTypeError,
    EvidenceItemRequiredError,
    EvidenceSizeLimitExceededError,
    EvidenceUploadResult,
    get_max_size_bytes,
    upload_evidence,
)
from engram.logbook.uri import build_attachment_evidence_uri


def compute_sha256(content: str) -> str:
    """计算内容的 SHA256 哈希"""
    return hashlib.sha256(content.encode()).hexdigest()


class TestEvidenceUploadBasic:
    """基础上传功能测试"""

    @patch("engram.gateway.evidence_store.get_artifact_store")
    @patch("engram.gateway.evidence_store.db_attach")
    def test_upload_text_content(self, mock_attach, mock_get_store):
        """测试上传普通文本内容"""
        content = "Hello, world!"
        expected_sha256 = compute_sha256(content)

        # 设置 mock 返回正确的 sha256
        mock_store = MagicMock()
        mock_store.put.return_value = {
            "uri": "attachments/evidence/20260129/abc123_12345678.txt",
            "sha256": expected_sha256,
            "size_bytes": len(content.encode()),
        }
        mock_get_store.return_value = mock_store
        mock_attach.return_value = 123

        # 执行上传
        result = upload_evidence(
            content=content,
            content_type="text/plain",
            actor_user_id="test-user",
            item_id=1,
        )

        # 验证结果
        assert isinstance(result, EvidenceUploadResult)
        assert result.attachment_id == 123
        assert result.content_type == "text/plain"
        assert result.sha256 == expected_sha256
        assert result.size_bytes == len(content.encode())

        # 验证调用
        mock_store.put.assert_called_once()
        mock_attach.assert_called_once()

    @patch("engram.gateway.evidence_store.get_artifact_store")
    @patch("engram.gateway.evidence_store.db_attach")
    def test_upload_markdown_content(self, mock_attach, mock_get_store):
        """测试上传 Markdown 内容"""
        content = "# Title\n\nSome content"
        expected_sha256 = compute_sha256(content)

        mock_store = MagicMock()
        mock_store.put.return_value = {
            "uri": "attachments/evidence/20260129/abc123_12345678.md",
            "sha256": expected_sha256,
            "size_bytes": len(content.encode()),
        }
        mock_get_store.return_value = mock_store
        mock_attach.return_value = 456

        result = upload_evidence(
            content=content,
            content_type="text/markdown",
            item_id=1,
        )

        assert result.content_type == "text/markdown"
        assert ".md" in result.artifact_uri

    @patch("engram.gateway.evidence_store.get_artifact_store")
    @patch("engram.gateway.evidence_store.db_attach")
    def test_upload_without_item_id_raises_error(self, mock_attach, mock_get_store):
        """测试不提供 item_id 时应抛出 EvidenceItemRequiredError"""
        content = "test"
        expected_sha256 = compute_sha256(content)

        mock_store = MagicMock()
        mock_store.put.return_value = {
            "uri": "attachments/evidence/test.txt",
            "sha256": expected_sha256,
            "size_bytes": len(content.encode()),
        }
        mock_get_store.return_value = mock_store

        with pytest.raises(EvidenceItemRequiredError) as exc_info:
            upload_evidence(
                content=content,
                content_type="text/plain",
                item_id=None,  # 不关联 item
            )

        # 验证错误信息
        error = exc_info.value
        assert error.error_code == "EVIDENCE_ITEM_REQUIRED"
        assert "suggestion" in error.details
        assert "create_item" in error.details["suggestion"]

        # 应该不调用 attach
        mock_attach.assert_not_called()

    @patch("engram.gateway.evidence_store.get_artifact_store")
    @patch("engram.gateway.evidence_store.db_attach")
    def test_to_evidence_object(self, mock_attach, mock_get_store):
        """测试转换为 evidence(v2) 对象"""
        content = "test content"
        expected_sha256 = compute_sha256(content)

        mock_store = MagicMock()
        mock_store.put.return_value = {
            "uri": "attachments/evidence/test.txt",
            "sha256": expected_sha256,
            "size_bytes": len(content.encode()),
        }
        mock_get_store.return_value = mock_store
        mock_attach.return_value = 789

        result = upload_evidence(
            content=content,
            content_type="text/plain",
            item_id=1,
        )

        evidence_obj = result.to_evidence_object(title="Test Evidence")

        # 验证 evidence(v2) 对象格式
        assert "uri" in evidence_obj
        assert "sha256" in evidence_obj
        assert evidence_obj["source_type"] == "artifact"
        assert evidence_obj["source_id"] == "789"
        assert evidence_obj["title"] == "Test Evidence"
        assert "timestamp" in evidence_obj

        # 验证 uri 使用 canonical 格式: memory://attachments/<id>/<sha256>
        assert evidence_obj["uri"].startswith("memory://attachments/")
        # 验证 URI 严格等于 Logbook build_attachment_evidence_uri 构造结果
        expected_uri = build_attachment_evidence_uri(789, expected_sha256)
        assert evidence_obj["uri"] == expected_uri


class TestEvidenceCanonicalUri:
    """验证成功上传时返回 canonical URI 格式"""

    @patch("engram.gateway.evidence_store.get_artifact_store")
    @patch("engram.gateway.evidence_store.db_attach")
    def test_successful_upload_returns_canonical_uri(self, mock_attach, mock_get_store):
        """测试成功上传后返回 memory://attachments/<id>/<sha256> 格式的 canonical URI"""
        content = "canonical uri test"
        expected_sha256 = compute_sha256(content)
        attachment_id = 42

        mock_store = MagicMock()
        mock_store.put.return_value = {
            "uri": "attachments/evidence/20260129/abc123_12345678.txt",
            "sha256": expected_sha256,
            "size_bytes": len(content.encode()),
        }
        mock_get_store.return_value = mock_store
        mock_attach.return_value = attachment_id

        result = upload_evidence(
            content=content,
            content_type="text/plain",
            item_id=1,
        )

        # 验证基本返回
        assert result.attachment_id == attachment_id
        assert result.sha256 == expected_sha256

        # 验证 to_evidence_object 返回的 canonical URI 格式
        evidence_obj = result.to_evidence_object()
        expected_canonical_uri = build_attachment_evidence_uri(attachment_id, expected_sha256)

        # URI 必须是 memory://attachments/<attachment_id>/<sha256> 格式
        assert evidence_obj["uri"] == expected_canonical_uri
        assert evidence_obj["uri"].startswith("memory://attachments/")
        assert str(attachment_id) in evidence_obj["uri"]
        assert expected_sha256 in evidence_obj["uri"]


class TestEvidenceUriConsistency:
    """验证 evidence URI 与 Logbook 构造结果一致性"""

    def test_evidence_uri_equals_logbook_build_result(self):
        """验证 to_evidence_object() 生成的 URI 严格等于 Logbook build_attachment_evidence_uri()"""
        # 测试多种 attachment_id 和 sha256 组合
        # 注意：sha256 必须为 64 位十六进制字符串
        test_cases = [
            (123, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
            (
                1,
                "a1b2c3d4e5f6789012345678901234567890123456789012345678901234abcd",
            ),  # 最小有效 attachment_id
            (
                999999,
                "DEADBEEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF12345678",
            ),  # 大 id + 大写 sha256 (64位)
        ]

        for attachment_id, sha256 in test_cases:
            result = EvidenceUploadResult(
                attachment_id=attachment_id,
                sha256=sha256,
                artifact_uri="test/artifact.txt",
                size_bytes=100,
                content_type="text/plain",
                created_at="2026-01-29T00:00:00Z",
            )

            evidence_obj = result.to_evidence_object()

            # 正常情况：应使用 canonical URI
            expected_uri = build_attachment_evidence_uri(attachment_id, sha256)
            assert evidence_obj["uri"] == expected_uri, (
                f"URI 不匹配: got {evidence_obj['uri']}, expected {expected_uri} (Logbook 构造结果)"
            )

    def test_logbook_uri_format_normalization(self):
        """验证 Logbook build_attachment_evidence_uri 的 sha256 规范化行为"""
        # Logbook 函数会对 sha256 做 strip().lower() 处理
        # sha256 必须为 64 位十六进制字符串
        attachment_id = 42
        sha256_upper = "ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890"

        result = EvidenceUploadResult(
            attachment_id=attachment_id,
            sha256=sha256_upper,
            artifact_uri="test/artifact.txt",
            size_bytes=100,
            content_type="text/plain",
            created_at="2026-01-29T00:00:00Z",
        )

        evidence_obj = result.to_evidence_object()
        expected_uri = build_attachment_evidence_uri(attachment_id, sha256_upper)

        # 验证 URI 严格一致
        assert evidence_obj["uri"] == expected_uri
        # 验证 sha256 被规范化为小写
        assert sha256_upper.lower() in expected_uri


class TestEvidenceUploadLimits:
    """大小限制测试"""

    def test_size_limit_exceeded(self):
        """测试内容超出大小限制"""
        # 创建超过默认限制的内容
        large_content = "x" * (DEFAULT_MAX_SIZE_BYTES + 1)

        with pytest.raises(EvidenceSizeLimitExceededError) as exc_info:
            upload_evidence(
                content=large_content,
                content_type="text/plain",
            )

        error = exc_info.value
        assert error.error_code == "EVIDENCE_SIZE_LIMIT_EXCEEDED"
        assert "suggestion" in error.details

    @patch.dict("os.environ", {"EVIDENCE_MAX_SIZE_BYTES": "100"})
    def test_custom_size_limit(self):
        """测试自定义大小限制"""
        assert get_max_size_bytes() == 100

        content_101 = "x" * 101
        with pytest.raises(EvidenceSizeLimitExceededError):
            upload_evidence(
                content=content_101,
                content_type="text/plain",
            )


class TestEvidenceContentType:
    """内容类型校验测试"""

    def test_invalid_content_type(self):
        """测试不支持的内容类型"""
        with pytest.raises(EvidenceContentTypeError) as exc_info:
            upload_evidence(
                content="test",
                content_type="application/octet-stream",  # 不支持
            )

        error = exc_info.value
        assert error.error_code == "EVIDENCE_CONTENT_TYPE_NOT_ALLOWED"
        assert "allowed_types" in error.details

    def test_allowed_content_types(self):
        """验证支持的内容类型列表"""
        expected_types = {
            "text/plain",
            "text/markdown",
            "text/x-diff",
            "text/x-patch",
            "application/json",
            "text/yaml",
        }

        for content_type in expected_types:
            assert content_type in ALLOWED_CONTENT_TYPES


class TestEvidenceUploadMCP:
    """MCP 端点集成测试"""

    @pytest.fixture
    def mock_env(self):
        """Mock 必要的环境变量"""
        env_vars = {
            "PROJECT_KEY": "test-project",
            "POSTGRES_DSN": "postgresql://test:test@localhost/test",
            "OPENMEMORY_BASE_URL": "http://localhost:8080",
        }
        with patch.dict("os.environ", env_vars):
            yield env_vars

    @pytest.fixture
    def mock_config(self, mock_env):
        """Mock 配置"""
        mock_cfg = MagicMock()
        mock_cfg.project_key = "test-project"
        mock_cfg.postgres_dsn = "postgresql://test:test@localhost/test"
        mock_cfg.default_team_space = "team:test-project"
        mock_cfg.unknown_actor_policy = "reject"
        mock_cfg.private_space_prefix = "private:"
        mock_cfg.validate_evidence_refs = False
        mock_cfg.logbook_check_on_startup = False
        mock_cfg.auto_migrate_on_startup = False
        mock_cfg.gateway_port = 8787
        mock_cfg.governance_admin_key = None

        with patch("engram.gateway.main.get_config", return_value=mock_cfg):
            yield mock_cfg

    @pytest.fixture
    def client(self, mock_config):
        """创建测试客户端"""
        from fastapi.testclient import TestClient

        from engram.gateway.main import app

        return TestClient(app)

    @pytest.mark.skip(reason="需要更新 DI 测试方式：logbook_adapter 通过 deps 获取")
    @patch("engram.gateway.evidence_store.get_artifact_store")
    @patch("engram.gateway.evidence_store.db_attach")
    def test_evidence_upload_without_item_id_auto_creates_item(
        self, mock_attach, mock_get_store, client, mock_config
    ):
        """
        不传 item_id 时，MCP 端点自动创建 item

        验证：
        - logbook_adapter.create_item 被调用
        - 返回包含 item_id, attachment_id, sha256, evidence.uri
        """
        content = "test content for auto create"
        expected_sha256 = compute_sha256(content)
        auto_created_item_id = 12345
        attachment_id = 999

        # TODO: 需要通过正确的 DI/patch 方式连接到被测路径
        mock_logbook_adapter = MagicMock()
        # Mock create_item 返回自动创建的 item_id
        mock_logbook_adapter.create_item.return_value = auto_created_item_id

        # Mock artifact store
        mock_store = MagicMock()
        mock_store.put.return_value = {
            "uri": "attachments/evidence/test.txt",
            "sha256": expected_sha256,
            "size_bytes": len(content.encode()),
        }
        mock_get_store.return_value = mock_store
        mock_attach.return_value = attachment_id

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "evidence_upload",
                    "arguments": {
                        "content": content,
                        "content_type": "text/plain",
                        "title": "Test Evidence",
                        # 不传 item_id
                    },
                },
                "id": 1,
            },
        )

        assert response.status_code == 200
        result = response.json()

        # 验证 JSON-RPC 响应格式
        assert result.get("jsonrpc") == "2.0"
        assert result.get("id") == 1
        assert result.get("error") is None

        # 解析内容
        import json

        content_result = result["result"]["content"]
        tool_result = json.loads(content_result[0]["text"])

        # 验证成功
        assert tool_result["ok"] is True

        # 验证返回字段
        assert tool_result["item_id"] == auto_created_item_id
        assert tool_result["attachment_id"] == attachment_id
        assert tool_result["sha256"] == expected_sha256

        # 验证 evidence 对象包含 uri
        assert "evidence" in tool_result
        assert "uri" in tool_result["evidence"]
        assert tool_result["evidence"]["uri"].startswith("memory://attachments/")

        # 验证 create_item 被调用
        mock_logbook_adapter.create_item.assert_called_once()

    @pytest.mark.skip(reason="需要更新 DI 测试方式：logbook_adapter 通过 deps 获取")
    @patch("engram.gateway.evidence_store.get_artifact_store")
    @patch("engram.gateway.evidence_store.db_attach")
    def test_evidence_upload_with_explicit_item_id_does_not_create_item(
        self, mock_attach, mock_get_store, client, mock_config
    ):
        """
        显式传 item_id 时，不调用 create_item

        验证：
        - logbook_adapter.create_item 不被调用
        - 使用传入的 item_id
        """
        # TODO: 需要通过正确的 DI/patch 方式连接到被测路径
        mock_logbook_adapter = MagicMock()
        content = "test content with explicit item"
        expected_sha256 = compute_sha256(content)
        explicit_item_id = 42
        attachment_id = 888

        # Mock artifact store
        mock_store = MagicMock()
        mock_store.put.return_value = {
            "uri": "attachments/evidence/test.txt",
            "sha256": expected_sha256,
            "size_bytes": len(content.encode()),
        }
        mock_get_store.return_value = mock_store
        mock_attach.return_value = attachment_id

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "evidence_upload",
                    "arguments": {
                        "content": content,
                        "content_type": "text/plain",
                        "title": "Test Evidence",
                        "item_id": explicit_item_id,  # 显式传入 item_id
                    },
                },
                "id": 1,
            },
        )

        assert response.status_code == 200
        result = response.json()

        # 解析内容
        import json

        content_result = result["result"]["content"]
        tool_result = json.loads(content_result[0]["text"])

        # 验证成功
        assert tool_result["ok"] is True
        assert tool_result["item_id"] == explicit_item_id
        assert tool_result["attachment_id"] == attachment_id
        assert tool_result["sha256"] == expected_sha256

        # 验证 evidence.uri 存在
        assert "evidence" in tool_result
        assert "uri" in tool_result["evidence"]

        # 验证 create_item 未被调用
        mock_logbook_adapter.create_item.assert_not_called()

    @patch("engram.gateway.evidence_store.get_artifact_store")
    @patch("engram.gateway.evidence_store.db_attach")
    def test_evidence_upload_via_mcp(self, mock_attach, mock_get_store, client, mock_config):
        """通过 MCP 端点测试 evidence_upload（带 item_id）"""
        content = "test"
        expected_sha256 = compute_sha256(content)

        mock_store = MagicMock()
        mock_store.put.return_value = {
            "uri": "attachments/evidence/test.txt",
            "sha256": expected_sha256,
            "size_bytes": len(content.encode()),
        }
        mock_get_store.return_value = mock_store
        mock_attach.return_value = 999

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "evidence_upload",
                    "arguments": {
                        "content": content,
                        "content_type": "text/plain",
                        "title": "Test Evidence",
                        "item_id": 1,  # 显式传入 item_id
                    },
                },
                "id": 1,
            },
        )

        assert response.status_code == 200
        result = response.json()

        # 验证 JSON-RPC 响应格式
        assert result.get("jsonrpc") == "2.0"
        assert result.get("id") == 1
        assert result.get("error") is None

        # 验证 content 数组格式
        content_result = result["result"]["content"]
        assert isinstance(content_result, list)
        assert len(content_result) == 1
        assert content_result[0]["type"] == "text"

        # 解析内容
        import json

        tool_result = json.loads(content_result[0]["text"])
        assert tool_result["ok"] is True
        assert "evidence" in tool_result
        assert "attachment_id" in tool_result
        assert "sha256" in tool_result
        assert "item_id" in tool_result

        # 验证 evidence.uri 存在
        assert "uri" in tool_result["evidence"]

    @pytest.mark.skip(reason="需要更新 DI 测试方式：logbook_adapter 通过 deps 获取")
    def test_evidence_upload_size_exceeded_via_mcp(self, client, mock_config):
        """
        通过 MCP 端点测试大小超限错误

        验证：
        - ok: false
        - error_code 存在
        - suggestion 存在
        """

        # 创建超过限制的内容
        large_content = "x" * (DEFAULT_MAX_SIZE_BYTES + 1)

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "evidence_upload",
                    "arguments": {
                        "content": large_content,
                        "content_type": "text/plain",
                    },
                },
                "id": 2,
            },
        )

        assert response.status_code == 200
        result = response.json()

        # 解析结果
        import json

        content = result["result"]["content"]
        tool_result = json.loads(content[0]["text"])

        # 验证失败响应格式
        assert tool_result["ok"] is False
        assert tool_result["error_code"] == "EVIDENCE_SIZE_LIMIT_EXCEEDED"
        assert "suggestion" in tool_result
        # suggestion 应该包含可操作的建议
        assert tool_result["suggestion"] is not None
        assert len(tool_result["suggestion"]) > 0

    @pytest.mark.skip(reason="需要更新 DI 测试方式：logbook_adapter 通过 deps 获取")
    def test_evidence_upload_invalid_content_type_via_mcp(self, client, mock_config):
        """
        通过 MCP 端点测试内容类型错误

        验证：
        - ok: false
        - error_code 存在
        - allowed_types 存在
        """

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "evidence_upload",
                    "arguments": {
                        "content": "test",
                        "content_type": "image/png",  # 不支持
                    },
                },
                "id": 3,
            },
        )

        assert response.status_code == 200
        result = response.json()

        import json

        content = result["result"]["content"]
        tool_result = json.loads(content[0]["text"])

        # 验证失败响应格式
        assert tool_result["ok"] is False
        assert tool_result["error_code"] == "EVIDENCE_CONTENT_TYPE_NOT_ALLOWED"
        assert "allowed_types" in tool_result
        # allowed_types 应该是非空列表
        assert isinstance(tool_result["allowed_types"], list)
        assert len(tool_result["allowed_types"]) > 0
        # 应该包含常见的文本类型
        assert "text/plain" in tool_result["allowed_types"]


class TestEvidenceUploadErrorMessages:
    """错误消息可诊断性测试"""

    def test_size_exceeded_error_has_suggestion(self):
        """大小超限错误应包含建议"""
        large_content = "x" * (DEFAULT_MAX_SIZE_BYTES + 1)

        with pytest.raises(EvidenceSizeLimitExceededError) as exc_info:
            upload_evidence(content=large_content, content_type="text/plain")

        error = exc_info.value
        assert "外部存储" in error.details["suggestion"]

    def test_content_type_error_lists_allowed_types(self):
        """内容类型错误应列出允许的类型"""
        with pytest.raises(EvidenceContentTypeError) as exc_info:
            upload_evidence(content="test", content_type="application/zip")

        error = exc_info.value
        assert len(error.details["allowed_types"]) > 0
        assert "text/plain" in error.details["allowed_types"]


class TestEvidenceUploadDependencyMissing:
    """
    evidence_store 依赖缺失场景测试

    验证当 evidence_store 导入失败时：
    1. execute_evidence_upload 返回结构化错误 DEPENDENCY_MISSING
    2. 不抛出异常到 app 工厂层
    3. 不影响 /health 端点
    """

    @pytest.mark.asyncio
    async def test_import_failure_returns_structured_error(self, monkeypatch):
        """
        导入失败时返回结构化错误 DEPENDENCY_MISSING

        验证：
        - ok: false
        - error_code: DEPENDENCY_MISSING
        - retryable: false
        - suggestion 包含安装指引
        """
        from tests.gateway.helpers import FailingImport, patch_sys_modules

        # 需要移除的模块列表
        modules_to_remove = [
            "engram.gateway.evidence_store",
            "engram.logbook.artifact_store",
            "engram.logbook.db",
            "engram.logbook.uri",
            "engram.gateway.handlers.evidence_upload",
        ]

        # 使用 patch_sys_modules 进行模拟
        with patch_sys_modules(
            replacements={
                "engram.gateway.evidence_store": FailingImport(
                    "No module named 'engram_logbook' (mocked)"
                ),
            },
            remove=modules_to_remove,
        ):
            from engram.gateway.handlers.evidence_upload import execute_evidence_upload

            # 创建 mock deps
            mock_deps = MagicMock()

            # 执行函数
            result = await execute_evidence_upload(
                content="test content",
                content_type="text/plain",
                deps=mock_deps,
            )

            # 验证返回结构化错误
            assert result["ok"] is False
            assert result["error_code"] == "DEPENDENCY_MISSING"
            assert result["retryable"] is False
            assert "engram_logbook" in result["message"]
            assert "suggestion" in result
            assert "pip install" in result["suggestion"]
            assert "details" in result
            assert result["details"]["missing_module"] == "engram_logbook"

    @pytest.mark.asyncio
    async def test_import_failure_does_not_raise_exception(self, monkeypatch):
        """
        导入失败时不抛出异常

        验证函数正常返回 dict，而不是抛出 ImportError
        """
        from tests.gateway.helpers import FailingImport, patch_sys_modules

        modules_to_remove = [
            "engram.gateway.evidence_store",
            "engram.logbook.artifact_store",
            "engram.logbook.db",
            "engram.logbook.uri",
            "engram.gateway.handlers.evidence_upload",
        ]

        with patch_sys_modules(
            replacements={
                "engram.gateway.evidence_store": FailingImport("Test import failure"),
            },
            remove=modules_to_remove,
        ):
            from engram.gateway.handlers.evidence_upload import execute_evidence_upload

            mock_deps = MagicMock()

            # 应该不抛出异常
            result = await execute_evidence_upload(
                content="test",
                content_type="text/plain",
                deps=mock_deps,
            )

            # 应该返回 dict
            assert isinstance(result, dict)
            assert result["ok"] is False

    def test_tool_result_error_code_dependency_missing_exists(self):
        """
        验证 ToolResultErrorCode.DEPENDENCY_MISSING 常量存在

        注意：DEPENDENCY_MISSING 属于业务层错误码 (ToolResultErrorCode)，
        不属于 JSON-RPC 层错误码 (McpErrorReason/ErrorReason)。

        边界说明：
        - McpErrorReason/ErrorReason: 用于 JSON-RPC error.data.reason
        - ToolResultErrorCode: 用于工具执行结果 result.error_code
        """
        from engram.gateway.result_error_codes import ToolResultErrorCode

        assert hasattr(ToolResultErrorCode, "DEPENDENCY_MISSING")
        assert ToolResultErrorCode.DEPENDENCY_MISSING == "DEPENDENCY_MISSING"
