# OpenMemory Vendoring 与补丁管理

本文档说明 OpenMemory 上游镜像的引入方式、补丁分级与升级/回滚策略。

---

## 1. 引入策略

- **默认**：使用已验证的上游镜像 `OPENMEMORY_IMAGE=ghcr.io/caviraoss/openmemory:v1.3.3`（见 `.env.example`）
- **可选**：自建镜像仓库并锁定版本
- **不建议**：直接在仓库中 vendoring 上游源码（除非有强监管需求）

> **上游现状提醒**：OpenMemory 仓库首页目前明确标注 “This project is currently being fully rewritten.”  
> 这意味着路由、响应结构、环境变量默认值和发布节奏都可能在次版本内变化。统一栈接入时不要依赖 `latest` 的隐式行为，至少应固定：
> - npm / Python SDK 版本
> - 镜像 tag 或 digest
> - 本仓库已验证的兼容层范围（见 `src/engram/gateway/openmemory_client.py`）

---

## 2. 补丁分级

| 等级 | 说明 | 示例 |
|------|------|------|
| L0 | 配置变更 | 环境变量、镜像 tag |
| L1 | 非侵入脚本 | 健康检查、启动包装脚本 |
| L2 | 轻量补丁 | 参数校验、可选字段兼容 |
| L3 | 深度改造 | 核心逻辑、存储结构 |

---

## 3. Freeze 与回滚

- **Freeze**：升级窗口内冻结上游版本，避免并发变更
- **回滚**：保持可回滚的上一稳定镜像 tag

### 升级前最低核查项

- 确认 `/health` 返回结构是否仍兼容（`status=ok` 或 `ok=true`）
- 确认搜索端点是 `/memory/query` 还是 `/memory/search`
- 确认列表分页参数是否仍接受 `limit/offset` 或仅接受 `l/u`
- 确认强化接口字段是 `memory_id/delta` 还是 `id/boost`
- 确认 wipe 能力是否仍有独立端点；若无，需保留逐条删除回退

---

## Appendix A: Category B 补丁的最小可上游 PR 划分

> 目标：将非侵入补丁拆分为最小可上游的 PR，降低长期维护成本。

- 只包含一个主题（功能/修复/文档）
- 避免跨多个子系统的耦合改动
- 附带回归测试或最小复现脚本

---

更新时间：2026-01-31
