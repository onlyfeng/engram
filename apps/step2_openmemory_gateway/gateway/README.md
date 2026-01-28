# gateway（实现骨架说明）

## 开发环境配置

**前置依赖**：Gateway 依赖 `engram_step1` 模块（统一错误码等），需先安装：

```bash
# 在 monorepo 根目录执行
pip install -e apps/step1_logbook_postgres/scripts
```

**安装 Gateway**：

```bash
cd apps/step2_openmemory_gateway/gateway
pip install -e .           # 开发环境可编辑安装
pip install -e ".[dev]"    # 含测试依赖
```

**Docker 环境**（已包含所有依赖）：

```bash
docker compose -f docker-compose.unified.yml up gateway
```

---

建议实现方式：
- Node.js（更贴近 MCP 生态）或 Python（若你们已有 MCP Python 框架）
- 对外：提供 MCP Server（/mcp）
- 对内：
  - 访问 Postgres（读取 governance.settings、写 governance.write_audit / logbook.outbox_memory）
  - 访问 OpenMemory HTTP API（store/query/reinforce）

最小实现优先级：
1) memory_store：开关/策略校验 + 写入/降级 + 审计
2) memory_query：统一查询 team + private（合并去重）
3) outbox_flush：补偿队列写回 OpenMemory

---

## /mcp HTTP 行为规范

### 支持的 HTTP 方法

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/mcp` | 处理 JSON-RPC 请求 |
| `OPTIONS` | `/mcp` | CORS 预检请求，返回 204 |
| `GET/PUT/DELETE` | `/mcp` | 返回 405 Method Not Allowed |

### CORS 响应头

```
Content-Type: application/json
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: POST,OPTIONS
Access-Control-Allow-Headers: Content-Type,Authorization,Mcp-Session-Id
```

### JSON-RPC 错误码映射

| 错误码 | HTTP 状态码 | 含义 | 触发场景 |
|--------|------------|------|----------|
| `-32600` | 400 | Invalid Request | 请求体为空、非 JSON 对象、无效 JSON 格式 |
| `-32600` | 405 | Method Not Allowed | 使用 GET/PUT/DELETE 等非 POST 方法 |
| `-32603` | 500 | Internal Server Error | 服务器内部异常 |

### 错误响应格式
```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32600,
    "message": "Request body must be a JSON object"
  },
  "id": null
}
```

---

## /mcp 请求/响应示例

### MCP 配置（Cursor 侧）
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

### 协议支持

Gateway `/mcp` 端点支持两种协议格式，通过请求体字段自动识别：

1. **标准 MCP JSON-RPC（推荐）**: 检测 `jsonrpc: "2.0"` 和 `method` 字段，符合 MCP 规范
2. **简化模式（verify/内部使用）**: 检测 `tool` 和 `arguments` 字段，用于脚本验证和内部调用

### 标准 MCP JSON-RPC 请求字段规范（推荐）

所有请求必须符合 JSON-RPC 2.0 / MCP 规范：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `jsonrpc` | string | ✅ | 固定值 `"2.0"` |
| `id` | number \| string | ✅ | 请求标识符，用于匹配响应 |
| `method` | string | ✅ | 方法名，见下方支持的方法 |
| `params` | object | ❌ | 方法参数对象 |

### 支持的 JSON-RPC 方法

| 方法 | 说明 |
|------|------|
| `tools/list` | 返回可用工具清单 |
| `tools/call` | 调用指定工具 |

---

### tools/list - 获取可用工具清单

#### 请求
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {}
}
```

#### 响应
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "memory_store",
        "description": "存储记忆到 OpenMemory，含策略校验、审计、失败降级到 outbox",
        "inputSchema": { "type": "object", "properties": {...}, "required": ["payload_md"] }
      },
      {
        "name": "memory_query",
        "description": "查询记忆，支持多空间搜索和过滤",
        "inputSchema": { "type": "object", "properties": {...}, "required": ["query"] }
      },
      {
        "name": "reliability_report",
        "description": "获取可靠性统计报告（只读）",
        "inputSchema": { "type": "object", "properties": {}, "required": [] }
      },
      {
        "name": "governance_update",
        "description": "更新治理设置（需鉴权）",
        "inputSchema": { "type": "object", "properties": {...}, "required": [] }
      }
    ]
  }
}
```

---

### tools/call - 调用工具

#### params 字段规范

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `params.name` | string | ✅ | 要调用的工具名称 |
| `params.arguments` | object | ✅ | 工具参数对象 |

### memory_store 工具调用

#### 请求示例
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "memory_store",
    "arguments": {
      "payload_md": "# 今日讨论\n- 确定了 API 版本策略\n- 决定使用 Gateway 模式",
      "target_space": "team",
      "meta_json": "{\"source\":\"cursor\",\"session_id\":\"abc123\"}"
    }
  }
}
```

#### 响应示例（成功写入）
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"action\":\"allow\",\"space_written\":\"team\",\"memory_id\":\"mem_7f3a2b1c\",\"evidence_refs\":[]}"
      }
    ]
  }
}
```

#### 响应示例（降级到 outbox）
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"action\":\"redirect\",\"space_written\":\"outbox\",\"memory_id\":null,\"evidence_refs\":[]}"
      }
    ]
  }
}
```

---

## content 结构规范

MCP 工具响应中的 `content` 字段遵循 MCP 协议的标准结构：

```typescript
interface ToolResult {
  content: ContentItem[];
  isError?: boolean;  // 可选，标记业务错误
}

interface ContentItem {
  type: "text" | "image" | "resource";
  text?: string;      // type="text" 时必填
  data?: string;      // type="image" 时为 base64 数据
  mimeType?: string;  // type="image" 时的 MIME 类型
}
```

### content 数组约定

| 索引 | 用途 | 格式 |
|------|------|------|
| `[0]` | 人类可读摘要 | 纯文本描述，用于 LLM 理解 |
| `[1]` | 机器解析数据 | JSON 字符串，包含详细结构化数据 |

示例（参考上游实现）：
```json
{
  "content": [
    { "type": "text", "text": "Stored memory mem_abc123 (primary=semantic)" },
    { "type": "text", "text": "{\"hsg\":{\"id\":\"mem_abc123\",\"primary_sector\":\"semantic\",\"sectors\":[\"semantic\",\"episodic\"]}}" }
  ]
}
```

---

## memory_store 参数与返回说明

### 参数定义
```
memory_store(payload_md, target_space?, meta_json?)
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `payload_md` | string | ✅ | 要存储的记忆内容，建议使用 Markdown 格式 |
| `target_space` | string | ❌ | 目标空间："team" \| "private"，默认根据 governance 策略决定 |
| `meta_json` | string | ❌ | 附加元数据，JSON 字符串格式，如来源、会话ID等 |

### 返回字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `action` | string | 执行动作："allow"（成功写入）\| "redirect"（降级到 outbox）\| "reject"（策略拒绝） |
| `space_written` | string | 实际写入的空间："team" \| "private" \| "outbox" |
| `memory_id` | string \| null | 成功写入时返回的记忆 ID，降级/拒绝时为 null |
| `evidence_refs` | array | 关联的证据引用列表（用于未来溯源功能） |

### 处理流程
1. **策略校验**：检查 `governance.settings` 中的 `team_write_enabled` 开关
2. **写入尝试**：调用 OpenMemory API 存储到目标空间
3. **降级处理**：若写入失败或策略禁止，写入 `logbook.outbox_memory` 队列
4. **审计记录**：所有操作记录到 `governance.write_audit` 表

---

## 简化模式（verify/内部使用）

简化模式无需 JSON-RPC 包装，适用于脚本验证、内部调用等场景。

### 请求字段规范

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tool` | string | ✅ | 工具名称：`memory_store` / `memory_query` / `reliability_report` / `governance_update` |
| `arguments` | object | ✅ | 工具参数对象，字段见下方定义 |

### memory_store arguments 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `payload_md` | string | ✅ | 记忆内容（Markdown 格式） |
| `target_space` | string | ❌ | 目标空间，默认 `team:<project>` |
| `meta_json` | object | ❌ | 附加元数据 |
| `kind` | string | ❌ | 知识类型：`FACT` / `PROCEDURE` / `PITFALL` / `DECISION` / `REVIEW_GUIDE` |
| `evidence_refs` | array | ❌ | 证据链引用（字符串数组） |
| `is_bulk` | boolean | ❌ | 是否批量提交，默认 `false` |
| `item_id` | integer | ❌ | 关联的 `logbook.items.item_id` |
| `actor_user_id` | string | ❌ | 执行操作的用户标识（用于审计） |

### memory_query arguments 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | ✅ | 查询文本 |
| `spaces` | array | ❌ | 搜索空间列表 |
| `filters` | object | ❌ | 过滤条件 |
| `top_k` | integer | ❌ | 返回结果数量，默认 `10` |

### 请求示例

```json
{
  "tool": "memory_store",
  "arguments": {
    "payload_md": "# 部署备忘\n- 使用 8787 端口\n- 依赖 postgres 和 OpenMemory",
    "target_space": "team:engram",
    "actor_user_id": "cursor-user-001"
  }
}
```

### 响应格式（成功）
```json
{
  "ok": true,
  "result": {
    "ok": true,
    "action": "allow",
    "space_written": "team:engram",
    "memory_id": "mem_7f3a2b1c",
    "evidence_refs": [],
    "message": null
  }
}
```

### 响应格式（错误）
```json
{
  "ok": false,
  "error": "未知工具: invalid_tool"
}
```

**注意**：简化模式不支持 `tools/list`，建议新集成使用标准 MCP JSON-RPC 格式。

---

## JSON-RPC 错误码

| 错误码 | 名称 | 说明 |
|--------|------|------|
| -32700 | Parse Error | JSON 解析失败 |
| -32600 | Invalid Request | 无效的 JSON-RPC 请求 |
| -32601 | Method Not Found | 方法/工具不存在 |
| -32602 | Invalid Params | 无效参数 |
| -32603 | Internal Error | 内部错误 |
| -32000 | Tool Execution Error | 工具执行失败 |
