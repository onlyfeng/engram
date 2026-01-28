# Cursor 侧 MCP 集成（推荐：连接 Gateway）

## 方案 A（推荐）：Cursor -> Memory Gateway -> OpenMemory
优点：
- 强制执行 team_write 开关与策略
- 统一审计与降级（写 Step1 outbox）
- 可逐步扩展更多工具（promotion、reinforce 合并等）

## Cursor 配置模板
见 templates/.mcp.json（把 url 指向 Gateway 的 /mcp）

---

## `.mcp.json` 配置规范

### 基础配置结构

```json
{
  "mcpServers": {
    "memory-gateway": {
      "type": "http",
      "url": "http://<server-ip>:<gateway-port>/mcp"
    }
  }
}
```

### 兼容性假设与约束

| 约束项 | 要求 | 说明 |
|--------|------|------|
| `type` | 必须为 `"http"` | Gateway 仅支持 HTTP 传输，不支持 stdio |
| `url` | 必须以 `/mcp` 结尾 | 标准 MCP 端点路径 |
| 协议 | HTTP 或 HTTPS | 生产环境建议使用 HTTPS |
| 认证 | 通过 Header 传递 | 使用 `Authorization` 头，Gateway 需配置对应中间件 |

### Cursor 行为假设

1. **请求格式**：Cursor 发送标准 JSON-RPC 2.0 请求
   - `method: "tools/call"` 用于工具调用
   - `method: "tools/list"` 用于获取工具列表
   - `method: "initialize"` 用于初始化握手

2. **Session 管理**：
   - Cursor 可能发送 `Mcp-Session-Id` 请求头
   - Gateway 需在 CORS 中允许该头部

3. **CORS 要求**：
   - Gateway 必须响应 `OPTIONS` 预检请求（返回 204）
   - 必须设置 `Access-Control-Allow-Origin: *`
   - 必须允许 `Content-Type`, `Authorization`, `Mcp-Session-Id` 头

### 配置示例（带认证）

```json
{
  "mcpServers": {
    "memory-gateway": {
      "type": "http",
      "url": "http://192.168.1.100:3001/mcp",
      "headers": {
        "Authorization": "Bearer <your-api-key>"
      }
    }
  }
}
```

### 常见问题排查

| 问题 | 可能原因 | 解决方案 |
|------|----------|----------|
| 连接失败 | CORS 未正确配置 | 检查 Gateway 是否响应 OPTIONS 请求 |
| 工具未显示 | `tools/list` 响应异常 | 检查 Gateway 日志，确认工具注册 |
| 认证失败 | Header 未传递 | 确认 `.mcp.json` 中配置了 `headers` |
| 超时 | 网络不通 | 检查防火墙和网络连通性 |

---

## Cursor 常见问题详解

### 问题 1: 405 Method Not Allowed / OPTIONS 请求失败

**现象**：
- Cursor 显示连接失败或工具不可用
- 浏览器开发者工具中看到 OPTIONS 请求返回 405

**原因**：
Cursor 在实际请求前会发送 CORS 预检（OPTIONS）请求。如果 Gateway 未正确处理 OPTIONS，浏览器会阻止后续请求。

**解决方案**：
确保 Gateway `/mcp` 端点正确响应 OPTIONS：
```
HTTP/1.1 204 No Content
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: POST,OPTIONS
Access-Control-Allow-Headers: Content-Type,Authorization,Mcp-Session-Id
```

**验证命令**：
```bash
curl -X OPTIONS http://<gateway-url>/mcp -i
# 应返回 204 状态码和上述 CORS 头
```

### 问题 2: 工具调用返回 content 结构不符，导致工具不可用

**现象**：
- 工具列表正常显示，但调用工具时报错
- Cursor 显示 "Tool execution failed" 或类似错误
- Gateway 日志显示请求成功，但 Cursor 无法解析响应

**原因**：
MCP 协议要求 `tools/call` 响应中的 `result.content` 必须是数组格式，且每个元素必须包含 `type` 和对应字段（如 `text`）。

**错误示例**（Cursor 无法解析）：
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": "存储成功"  // ❌ 错误：content 应为数组
  }
}
```

**正确格式**：
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      { "type": "text", "text": "存储成功，memory_id=mem_abc123" }
    ]
  }
}
```

**检查要点**：
1. `content` 必须是数组 `[]`
2. 数组元素必须包含 `type` 字段（通常为 `"text"`）
3. `type: "text"` 时必须有 `text` 字段
4. 业务错误应使用 `isError: true` 标记，而非返回 JSON-RPC error

### 问题 3: tools/list 返回空列表

**现象**：
- Cursor MCP 面板中显示已连接，但无工具可用

**可能原因**：
1. Gateway 尚未注册工具
2. `tools/list` 响应格式错误
3. 响应中 `result.tools` 不是数组

**正确响应格式**：
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "memory_store",
        "description": "存储记忆到 OpenMemory",
        "inputSchema": {
          "type": "object",
          "properties": { ... },
          "required": ["payload_md"]
        }
      }
    ]
  }
}
```

### 问题 4: Session 管理异常

**现象**：
- 首次调用成功，后续调用失败
- 出现 session 不匹配错误

**原因**：
Cursor 可能发送 `Mcp-Session-Id` 请求头进行会话管理。如果 Gateway 强制要求该头但未正确处理，会导致请求失败。

**建议**：
- Gateway 应允许 `Mcp-Session-Id` 头（在 CORS 中）
- 但不应强制要求该头存在（保持向后兼容）

---

## 协议支持说明

Gateway `/mcp` 端点支持两种协议格式，通过请求体字段自动识别：

| 协议格式 | 识别特征 | 适用场景 |
|----------|----------|----------|
| **标准 MCP JSON-RPC** | 包含 `jsonrpc: "2.0"` 和 `method` 字段 | Cursor、标准 MCP 客户端（推荐） |
| **简化模式** | 包含 `tool` 和 `arguments` 字段 | 脚本验证、内部调用 |

详细协议规范见 `gateway/README.md`。

### HTTP 端点行为参考

遵循上游 OpenMemory MCP 实现的 HTTP 行为：

- `POST /mcp`：处理 JSON-RPC 请求
- `OPTIONS /mcp`：CORS 预检，返回 204 + CORS 头
- `GET/PUT/DELETE /mcp`：返回 405，错误码 `-32600`

详细错误码映射见 `gateway/README.md`。
