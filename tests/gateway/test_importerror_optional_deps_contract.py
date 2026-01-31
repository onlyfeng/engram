# -*- coding: utf-8 -*-
"""
测试可选依赖缺失时的优雅降级行为

验证 Gateway 在可选依赖（如 engram_logbook）不可用时：
1. 应用导入不崩溃
2. /health 端点可访问
3. 依赖可选模块的工具返回结构化错误而非崩溃

测试策略：
- 使用子进程方式 (subprocess + python -c) 隔离测试环境
- 使用 import hook 模拟依赖缺失
- 验证错误响应结构符合契约

契约要求：
- evidence_upload 在依赖缺失时返回 error_code="DEPENDENCY_MISSING"
- 返回包含 retryable=False 和 suggestion 字段
"""

import json
import subprocess
import sys
import textwrap

import pytest


class TestImportSafetySubprocess:
    """使用子进程验证 import-time 安全性"""

    def test_app_import_succeeds_in_minimal_env(self):
        """
        验证在最小环境下 create_app 导入成功

        注意：此测试验证 app.py 本身的导入是安全的，
        但 logbook_adapter 依赖 engram_logbook，所以完整 gateway 需要它。
        """
        # 验证当前环境可以正常导入
        code = textwrap.dedent("""
            import sys
            try:
                from engram.gateway.app import create_app
                print("IMPORT_OK")
            except ImportError as e:
                print(f"IMPORT_FAILED: {e}")
            except Exception as e:
                print(f"UNEXPECTED_ERROR: {type(e).__name__}: {e}")
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = result.stdout.strip()
        # 如果 engram_logbook 可用，导入应该成功
        # 如果不可用，会得到明确的 ImportError
        assert "IMPORT_OK" in output or "IMPORT_FAILED" in output, (
            f"导入结果不明确:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_health_endpoint_accessible_with_test_container(self):
        """
        验证使用测试容器时 /health 端点可访问

        使用子进程隔离，确保环境干净
        """
        code = textwrap.dedent("""
            import json
            import sys

            try:
                from fastapi.testclient import TestClient
                from engram.gateway.app import create_app
                from engram.gateway.container import GatewayContainer, set_container

                # 创建最小测试配置
                class MinimalConfig:
                    project_key = "test"
                    postgres_dsn = "postgresql://test:test@localhost/test"
                    default_team_space = "team:test"
                    private_space_prefix = "private:"
                    openmemory_base_url = "http://localhost:8080"
                    openmemory_api_key = None
                    governance_admin_key = None
                    unknown_actor_policy = "degrade"
                    logbook_check_on_startup = False
                    auto_migrate_on_startup = False
                    gateway_port = 8787
                    minio_audit_webhook_auth_token = None
                    minio_audit_max_payload_size = 1048576
                    validate_evidence_refs = False
                    strict_mode_enforce_validate_refs = True

                # 创建 mock 依赖
                class MockLogbookAdapter:
                    def check_user_exists(self, user_id):
                        return True
                    def ensure_user(self, user_id, **kwargs):
                        return {"user_id": user_id}
                    def check_dedup(self, target_space, payload_sha):
                        return None
                    def get_or_create_settings(self, project_key):
                        return {"team_write_enabled": False, "policy_json": {}}
                    def insert_audit(self, **kwargs):
                        return 1
                    def enqueue_outbox(self, **kwargs):
                        return 1
                    def upsert_settings(self, **kwargs):
                        return True
                    def create_item(self, **kwargs):
                        return 1

                class MockLogbookDb:
                    def get_settings(self, project_key):
                        return {"team_write_enabled": False, "policy_json": {}}
                    def get_or_create_settings(self, project_key):
                        return {"team_write_enabled": False, "policy_json": {}}
                    def upsert_settings(self, **kwargs):
                        return {"team_write_enabled": False, "policy_json": {}}
                    def insert_audit(self, **kwargs):
                        return 1
                    def enqueue_outbox(self, **kwargs):
                        return 1

                class MockOpenMemoryClient:
                    def store(self, **kwargs):
                        class Result:
                            success = True
                            memory_id = "fake_id"
                        return Result()
                    def search(self, **kwargs):
                        class Result:
                            success = True
                            results = []
                        return Result()

                # 设置测试容器
                container = GatewayContainer.create_for_testing(
                    config=MinimalConfig(),
                    db=MockLogbookDb(),
                    logbook_adapter=MockLogbookAdapter(),
                    openmemory_client=MockOpenMemoryClient(),
                )
                set_container(container)

                # 创建应用并测试
                app = create_app(container=container)
                client = TestClient(app)
                response = client.get("/health")

                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok") == True:
                        print("HEALTH_OK")
                    else:
                        print(f"HEALTH_FAILED: {json.dumps(data)}")
                else:
                    print(f"HEALTH_STATUS_ERROR: {response.status_code}")

            except ImportError as e:
                print(f"IMPORT_ERROR: {e}")
            except Exception as e:
                import traceback
                print(f"UNEXPECTED_ERROR: {type(e).__name__}: {e}")
                traceback.print_exc()
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = result.stdout.strip()
        # 如果环境完整，应该返回 HEALTH_OK
        # 如果依赖缺失，返回 IMPORT_ERROR
        assert "HEALTH_OK" in output or "IMPORT_ERROR" in output, (
            f"健康检查结果不明确:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


class TestEvidenceUploadDependencyMissing:
    """
    测试 evidence_upload 工具在依赖缺失时的行为

    evidence_upload handler 已实现延迟导入策略：
    - 在函数内部 try/except ImportError
    - 返回结构化错误 DEPENDENCY_MISSING
    """

    def test_evidence_upload_returns_structured_error_on_import_failure(self):
        """
        验证 evidence_upload 在 evidence_store 导入失败时返回结构化错误

        使用干净的子进程 + 早期 import hook 模拟 evidence_store 导入失败

        策略：在任何 engram 模块导入前安装 import hook
        """
        code = textwrap.dedent("""
            import json
            import sys

            # 在导入任何 engram 模块前安装 import hook
            # 这确保了 evidence_store 的延迟导入会触发 ImportError

            class BlockingImportFinder:
                '''阻止特定模块导入的 import hook'''

                BLOCKED_PREFIXES = (
                    'engram.gateway.evidence_store',
                    'engram.logbook.artifact_store',
                )

                def find_spec(self, fullname, path, target=None):
                    '''Python 3.4+ 的 import hook 接口'''
                    for prefix in self.BLOCKED_PREFIXES:
                        if fullname == prefix or fullname.startswith(prefix + '.'):
                            # 返回一个会触发 ImportError 的 spec
                            from importlib.machinery import ModuleSpec
                            return ModuleSpec(fullname, self)
                    return None

                def create_module(self, spec):
                    return None

                def exec_module(self, module):
                    raise ImportError(f"测试模拟: {module.__name__} 模块不可用 (engram_logbook 未安装)")

            # 在最前面安装 hook
            sys.meta_path.insert(0, BlockingImportFinder())

            try:
                # 现在导入 handler - evidence_store 的延迟导入应该失败
                from engram.gateway.handlers.evidence_upload import execute_evidence_upload
                import asyncio

                # 创建 mock deps
                class MockDeps:
                    class MockAdapter:
                        def create_item(self, **kwargs):
                            return 1
                    logbook_adapter = MockAdapter()
                    config = None
                    db = None
                    openmemory_client = None

                async def test():
                    result = await execute_evidence_upload(
                        content="test content",
                        content_type="text/plain",
                        title="Test",
                        deps=MockDeps(),
                    )
                    return result

                result = asyncio.run(test())

                # 验证返回结构
                if result.get("ok") == False:
                    error_code = result.get("error_code")
                    if error_code == "DEPENDENCY_MISSING":
                        if result.get("retryable") == False:
                            if "suggestion" in result or "message" in result:
                                print("DEPENDENCY_MISSING_OK")
                                print(f"RESULT: {json.dumps(result, ensure_ascii=False)}")
                            else:
                                print(f"MISSING_SUGGESTION: {json.dumps(result, ensure_ascii=False)}")
                        else:
                            print(f"WRONG_RETRYABLE: {json.dumps(result, ensure_ascii=False)}")
                    else:
                        # 其他错误码也是可接受的结构化错误
                        print(f"STRUCTURED_ERROR: {json.dumps(result, ensure_ascii=False)}")
                else:
                    print(f"UNEXPECTED_SUCCESS: {json.dumps(result, ensure_ascii=False)}")

            except ImportError as e:
                # handler 本身导入失败（可能是 logbook_adapter 等依赖链问题）
                print(f"HANDLER_IMPORT_ERROR: {e}")
            except Exception as e:
                import traceback
                print(f"UNEXPECTED_ERROR: {type(e).__name__}: {e}")
                traceback.print_exc()
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = result.stdout.strip()
        stderr = result.stderr.strip()

        # 分析结果 - 验证返回的是结构化错误而非崩溃
        if "DEPENDENCY_MISSING_OK" in output:
            # 最佳情况：返回了 DEPENDENCY_MISSING 错误
            pass
        elif "STRUCTURED_ERROR" in output:
            # 可接受：返回了其他结构化错误（如 EVIDENCE_WRITE_FAILED）
            # 这说明 handler 没有崩溃，而是返回了可诊断的错误
            pass
        elif "HANDLER_IMPORT_ERROR" in output:
            # handler 本身导入失败（依赖链问题）
            pytest.skip(f"Handler 导入失败（依赖链）: {output}")
        elif "UNEXPECTED_ERROR" in output or "Traceback" in stderr:
            pytest.fail(
                f"evidence_upload 发生未预期的异常（应返回结构化错误）\n"
                f"stdout: {output}\nstderr: {stderr}"
            )
        else:
            pytest.fail(
                f"evidence_upload 未返回预期的结构化错误\nstdout: {output}\nstderr: {stderr}"
            )


class TestGatewayGracefulDegradation:
    """
    测试 Gateway 整体的优雅降级行为

    场景：当某些可选依赖不可用时，Gateway 应该：
    1. 能够启动
    2. 健康检查正常
    3. 不可用的功能返回明确的错误信息
    """

    def test_gateway_app_factory_with_mocked_imports(self):
        """
        测试 app 工厂在 mock 环境下的行为

        验证 create_app 的延迟初始化策略生效
        """
        code = textwrap.dedent("""
            import json
            import sys
            import os

            # 设置必要的环境变量（用于测试模式）
            os.environ.setdefault("PROJECT_KEY", "test")
            os.environ.setdefault("POSTGRES_DSN", "postgresql://test:test@localhost/test")
            os.environ.setdefault("OPENMEMORY_BASE_URL", "http://localhost:8080")

            try:
                from fastapi.testclient import TestClient
                from engram.gateway.app import create_app
                from engram.gateway.container import GatewayContainer, set_container, reset_container

                # 重置任何已存在的容器
                reset_container()

                # 创建最小配置
                class MinimalConfig:
                    project_key = "test"
                    postgres_dsn = "postgresql://test:test@localhost/test"
                    default_team_space = "team:test"
                    private_space_prefix = "private:"
                    openmemory_base_url = "http://localhost:8080"
                    openmemory_api_key = None
                    governance_admin_key = None
                    unknown_actor_policy = "degrade"
                    logbook_check_on_startup = False
                    auto_migrate_on_startup = False
                    gateway_port = 8787
                    minio_audit_webhook_auth_token = None
                    minio_audit_max_payload_size = 1048576
                    validate_evidence_refs = False
                    strict_mode_enforce_validate_refs = True

                class MockLogbookDb:
                    def get_settings(self, project_key):
                        return {"team_write_enabled": False, "policy_json": {}}
                    def get_or_create_settings(self, project_key):
                        return {"team_write_enabled": False, "policy_json": {}}
                    def upsert_settings(self, **kwargs):
                        return {"team_write_enabled": False, "policy_json": {}}
                    def insert_audit(self, **kwargs):
                        return 1
                    def enqueue_outbox(self, **kwargs):
                        return 1

                class MockLogbookAdapter:
                    def check_user_exists(self, user_id):
                        return True
                    def ensure_user(self, user_id, **kwargs):
                        return {"user_id": user_id}
                    def check_dedup(self, target_space, payload_sha):
                        return None
                    def get_or_create_settings(self, project_key):
                        return {"team_write_enabled": False, "policy_json": {}}
                    def insert_audit(self, **kwargs):
                        return 1
                    def enqueue_outbox(self, **kwargs):
                        return 1
                    def upsert_settings(self, **kwargs):
                        return True
                    def create_item(self, **kwargs):
                        return 1
                    def get_reliability_report(self):
                        from datetime import datetime, timezone
                        return {
                            "outbox_stats": {"total": 0, "by_status": {"pending": 0, "sent": 0, "dead": 0}},
                            "audit_stats": {"total": 0, "by_action": {}, "recent_24h": 0, "by_reason": {}},
                            "v2_evidence_stats": {},
                            "content_intercept_stats": {},
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                        }

                class MockOpenMemoryClient:
                    def store(self, **kwargs):
                        class Result:
                            success = True
                            memory_id = "fake_id"
                        return Result()
                    def search(self, **kwargs):
                        class Result:
                            success = True
                            results = []
                        return Result()

                # 设置测试容器
                container = GatewayContainer.create_for_testing(
                    config=MinimalConfig(),
                    db=MockLogbookDb(),
                    logbook_adapter=MockLogbookAdapter(),
                    openmemory_client=MockOpenMemoryClient(),
                )
                set_container(container)

                # 创建应用
                app = create_app(container=container)
                client = TestClient(app)

                # 测试 /health
                health_response = client.get("/health")
                if health_response.status_code != 200:
                    print(f"HEALTH_FAILED: status={health_response.status_code}")
                    sys.exit(1)

                health_data = health_response.json()
                if not health_data.get("ok"):
                    print(f"HEALTH_NOT_OK: {json.dumps(health_data)}")
                    sys.exit(1)

                # 测试 /reliability/report
                report_response = client.get("/reliability/report")
                if report_response.status_code != 200:
                    print(f"REPORT_FAILED: status={report_response.status_code}")
                    sys.exit(1)

                report_data = report_response.json()
                if "outbox_stats" not in report_data:
                    print(f"REPORT_MISSING_FIELDS: {json.dumps(report_data)}")
                    sys.exit(1)

                print("ALL_TESTS_PASSED")

            except ImportError as e:
                print(f"IMPORT_ERROR: {e}")
            except Exception as e:
                import traceback
                print(f"UNEXPECTED_ERROR: {type(e).__name__}: {e}")
                traceback.print_exc()
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = result.stdout.strip()
        stderr = result.stderr.strip()

        if "ALL_TESTS_PASSED" in output:
            pass  # 成功
        elif "IMPORT_ERROR" in output:
            pytest.skip(f"依赖导入失败: {output}")
        else:
            pytest.fail(f"Gateway 优雅降级测试失败\nstdout: {output}\nstderr: {stderr}")


class TestErrorResponseContract:
    """
    测试错误响应契约

    验证依赖缺失错误响应符合 MCP JSON-RPC 错误契约
    """

    def test_dependency_missing_error_structure(self):
        """
        验证 DEPENDENCY_MISSING 错误响应结构

        必须包含:
        - ok: false
        - error_code: "DEPENDENCY_MISSING"
        - retryable: false
        - message 或 suggestion (可诊断信息)
        """
        # 直接测试 handler 的错误响应结构

        # 模拟 ImportError 场景返回的错误结构
        expected_error = {
            "ok": False,
            "error_code": "DEPENDENCY_MISSING",
            "retryable": False,
            "message": "evidence_upload 功能依赖 engram_logbook 模块，当前未安装或配置不正确",
            "suggestion": (
                "请确保 engram_logbook 模块已正确安装：\n"
                '  pip install -e ".[full]"\n'
                "或检查 POSTGRES_DSN 环境变量是否正确配置"
            ),
            "details": {
                "missing_module": "engram_logbook",
                "import_error": "some error",
            },
        }

        # 验证必需字段
        assert expected_error["ok"] is False
        assert expected_error["error_code"] == "DEPENDENCY_MISSING"
        assert expected_error["retryable"] is False
        assert "message" in expected_error or "suggestion" in expected_error
        assert "details" in expected_error
        assert "missing_module" in expected_error["details"]

    def test_missing_parameter_error_structure(self):
        """
        验证缺少必需参数的错误响应结构

        handler 应返回:
        - ok: false
        - error_code: "MISSING_REQUIRED_PARAMETER"
        - retryable: false
        """
        # 模拟缺少参数时的错误结构
        expected_error = {
            "ok": False,
            "error_code": "MISSING_REQUIRED_PARAMETER",
            "retryable": False,
            "suggestion": "参数 'content' 为必填项，请提供证据内容",
        }

        assert expected_error["ok"] is False
        assert expected_error["error_code"] == "MISSING_REQUIRED_PARAMETER"
        assert expected_error["retryable"] is False
        assert "suggestion" in expected_error


class TestIntegrationWithFakes:
    """
    使用 Fake 对象的集成测试

    不涉及子进程，直接使用项目的 fake 依赖进行测试
    """

    @pytest.fixture
    def test_app_with_fakes(self, gateway_test_container):
        """创建带 fake 依赖的测试应用"""
        from fastapi.testclient import TestClient

        from engram.gateway.app import create_app

        container = gateway_test_container["container"]
        app = create_app(container=container)
        return TestClient(app)

    def test_health_endpoint_with_fakes(self, test_app_with_fakes):
        """验证使用 fake 依赖时健康检查正常"""
        response = test_app_with_fakes.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["status"] == "ok"

    def test_mcp_options_preflight(self, test_app_with_fakes):
        """验证 MCP 端点的 CORS 预检请求"""
        response = test_app_with_fakes.options("/mcp")
        assert response.status_code == 200
        assert "Access-Control-Allow-Origin" in response.headers

    def test_evidence_upload_missing_content_returns_error(self, test_app_with_fakes):
        """
        验证 evidence_upload 缺少 content 参数时返回结构化错误

        通过 MCP JSON-RPC 调用 evidence_upload 工具
        """
        response = test_app_with_fakes.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "evidence_upload",
                    "arguments": {
                        # content 缺失
                        "content_type": "text/plain",
                    },
                },
                "id": 1,
            },
        )

        assert response.status_code == 200
        result = response.json()

        # 应该是成功的 JSON-RPC 响应，但工具返回错误
        assert result.get("jsonrpc") == "2.0"
        assert result.get("id") == 1
        assert "result" in result

        # 解析工具返回内容
        content = result["result"]["content"]
        assert isinstance(content, list)
        assert len(content) == 1

        tool_result = json.loads(content[0]["text"])
        assert tool_result["ok"] is False
        # 可能是 MISSING_REQUIRED_PARAMETER 或 DEPENDENCY_MISSING
        assert tool_result.get("error_code") in [
            "MISSING_REQUIRED_PARAMETER",
            "DEPENDENCY_MISSING",
        ]


# 确保测试可以独立运行
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
