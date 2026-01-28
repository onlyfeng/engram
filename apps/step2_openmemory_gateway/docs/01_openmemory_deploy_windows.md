# OpenMemory 后端部署（Windows 内网 / 非 Docker）

目标：在服务器上常驻运行 OpenMemory backend（HTTP + MCP + dashboard），连接项目 Postgres。

建议方式：
- Node.js LTS（18/20）
- 以 Windows Service 方式运行（例如使用 NSSM）
- 后端只由服务器访问数据库；客户端（Cursor）只访问 HTTP/MCP

## 运行要点
- HTTP：默认 8080（建议内网反代或直接开放给办公网段）
- MCP：/mcp
- Dashboard：同 HTTP 下

## 安全建议（即便内网）
- 启用 API Key/Token（由 Gateway 或客户端携带）
- 端口限制网段访问（Windows 防火墙）
- 日志保留至少 7 天，用于审计与故障定位

备注：OpenMemory 为 Apache-2.0 协议，适合企业内部二次集成。
