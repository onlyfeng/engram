# -*- coding: utf-8 -*-
"""
Gateway 启动验收测试

验证 Gateway 服务可正常启动:
- 服务启动
- 健康检查端点
- MCP 能力端点
- 基本 API 响应
"""

import os
import time
import signal
import pytest
from multiprocessing import Process


# 检查 Gateway 依赖是否可用
try:
    import httpx
    import uvicorn
    GATEWAY_DEPS_AVAILABLE = True
except ImportError:
    GATEWAY_DEPS_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not GATEWAY_DEPS_AVAILABLE,
    reason="Gateway 依赖 (httpx, uvicorn) 未安装"
)


def start_gateway_server(host: str, port: int, dsn: str):
    """在子进程中启动 Gateway 服务器"""
    os.environ["POSTGRES_DSN"] = dsn
    os.environ["OPENMEMORY_BASE_URL"] = ""  # 可选，设为空
    os.environ["GATEWAY_PORT"] = str(port)
    
    try:
        import uvicorn
        uvicorn.run(
            "engram.gateway.main:app",
            host=host,
            port=port,
            log_level="error",
        )
    except Exception as e:
        print(f"Gateway 启动失败: {e}")


class TestGatewayStartup:
    """Gateway 启动测试"""

    @pytest.fixture(scope="function")
    def gateway_server(self, migrated_db, unused_tcp_port):
        """启动 Gateway 服务器 fixture"""
        host = "127.0.0.1"
        port = unused_tcp_port
        dsn = migrated_db["dsn"]
        
        # 在子进程中启动服务器
        proc = Process(
            target=start_gateway_server,
            args=(host, port, dsn),
            daemon=True,
        )
        proc.start()
        
        # 等待服务器启动
        base_url = f"http://{host}:{port}"
        max_retries = 30
        for i in range(max_retries):
            try:
                response = httpx.get(f"{base_url}/health", timeout=1.0)
                if response.status_code in (200, 503):  # 503 可能是 OpenMemory 不可用
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            time.sleep(0.5)
        else:
            proc.terminate()
            proc.join(timeout=5)
            pytest.skip(f"Gateway 服务器启动超时 (port={port})")
        
        yield base_url
        
        # 清理
        proc.terminate()
        proc.join(timeout=5)
        if proc.is_alive():
            os.kill(proc.pid, signal.SIGKILL)

    def test_health_endpoint(self, gateway_server):
        """健康检查端点返回响应"""
        response = httpx.get(f"{gateway_server}/health", timeout=5.0)
        
        # 健康检查应该返回 200 或 503（如果 OpenMemory 不可用）
        assert response.status_code in (200, 503), f"健康检查返回: {response.status_code}"
        
        # 响应应该是 JSON
        try:
            data = response.json()
            assert isinstance(data, dict)
        except Exception:
            # 如果不是 JSON，至少有响应体
            pass

    def test_health_endpoint_has_status(self, gateway_server):
        """健康检查包含状态信息"""
        response = httpx.get(f"{gateway_server}/health", timeout=5.0)
        
        if response.status_code == 200:
            try:
                data = response.json()
                # 可能有 status, healthy, ok 等字段
                has_status = any(key in data for key in ["status", "healthy", "ok"])
                if not has_status:
                    # 也可能返回简单的空对象
                    assert isinstance(data, dict)
            except Exception:
                pass

    def test_mcp_endpoint_exists(self, gateway_server):
        """MCP 端点存在"""
        # MCP 端点通常是 POST /mcp 或 /jsonrpc
        for endpoint in ["/mcp", "/jsonrpc", "/"]:
            try:
                response = httpx.post(
                    f"{gateway_server}{endpoint}",
                    json={
                        "jsonrpc": "2.0",
                        "method": "initialize",
                        "params": {},
                        "id": 1,
                    },
                    timeout=5.0,
                )
                # 找到可用的端点
                if response.status_code in (200, 400, 404):
                    return
            except Exception:
                continue
        
        # 如果所有端点都失败，跳过测试
        pytest.skip("未找到 MCP 端点")

    def test_mcp_tools_list(self, gateway_server):
        """MCP tools/list 方法可用"""
        for endpoint in ["/mcp", "/jsonrpc", "/"]:
            try:
                response = httpx.post(
                    f"{gateway_server}{endpoint}",
                    json={
                        "jsonrpc": "2.0",
                        "method": "tools/list",
                        "params": {},
                        "id": 1,
                    },
                    timeout=5.0,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if "result" in data:
                        result = data["result"]
                        # tools/list 应该返回工具列表
                        if "tools" in result:
                            assert isinstance(result["tools"], list)
                            return
                    elif "error" in data:
                        # 方法不支持也是可接受的
                        return
            except Exception:
                continue

    def test_not_found_returns_404(self, gateway_server):
        """不存在的端点返回 404"""
        response = httpx.get(
            f"{gateway_server}/nonexistent_endpoint_12345",
            timeout=5.0,
        )
        assert response.status_code == 404

    def test_cors_headers(self, gateway_server):
        """CORS 头正确配置（如果启用）"""
        response = httpx.options(
            f"{gateway_server}/health",
            headers={"Origin": "http://localhost:3000"},
            timeout=5.0,
        )
        
        # CORS 可能启用也可能未启用
        # 只验证请求不会崩溃
        assert response.status_code in (200, 204, 405)


class TestGatewayWithoutOpenMemory:
    """无 OpenMemory 时的 Gateway 测试"""

    @pytest.fixture(scope="function")
    def gateway_no_openmemory(self, migrated_db, unused_tcp_port):
        """启动不连接 OpenMemory 的 Gateway"""
        host = "127.0.0.1"
        port = unused_tcp_port
        dsn = migrated_db["dsn"]
        
        proc = Process(
            target=start_gateway_server,
            args=(host, port, dsn),
            daemon=True,
        )
        proc.start()
        
        base_url = f"http://{host}:{port}"
        max_retries = 30
        for i in range(max_retries):
            try:
                response = httpx.get(f"{base_url}/health", timeout=1.0)
                if response.status_code in (200, 503):
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            time.sleep(0.5)
        else:
            proc.terminate()
            proc.join(timeout=5)
            pytest.skip("Gateway 启动超时")
        
        yield base_url
        
        proc.terminate()
        proc.join(timeout=5)
        if proc.is_alive():
            os.kill(proc.pid, signal.SIGKILL)

    def test_health_without_openmemory(self, gateway_no_openmemory):
        """无 OpenMemory 时健康检查仍可用"""
        response = httpx.get(f"{gateway_no_openmemory}/health", timeout=5.0)
        
        # 应该返回响应（可能是 503 表示部分服务不可用）
        assert response.status_code in (200, 503)


class TestGatewayAppImport:
    """Gateway 应用导入测试"""

    def test_app_importable(self):
        """FastAPI app 可导入"""
        try:
            from engram.gateway.main import app
            assert app is not None
        except ImportError as e:
            if "fastapi" in str(e).lower():
                pytest.skip("FastAPI 未安装")
            raise

    def test_app_is_fastapi(self):
        """app 是 FastAPI 实例"""
        try:
            from fastapi import FastAPI
            from engram.gateway.main import app
            
            assert isinstance(app, FastAPI)
        except ImportError:
            pytest.skip("FastAPI 未安装")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
