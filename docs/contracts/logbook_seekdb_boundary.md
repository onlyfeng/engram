# Logbook ↔ SeekDB 边界契约

本文档定义 Logbook（事实账本）与 SeekDB（证据索引）之间的数据依赖、职责边界与禁用开关。

---

## 边界说明

| 组件 | 职责 |
|------|------|
| Logbook | 事实与证据的权威来源，提供可追溯 URI/证据引用 |
| SeekDB | 证据索引与检索层，消费 Logbook 输出的证据引用 |

---

## 输入/输出约束

- **输入**：Evidence Packet（见 [evidence_packet.md](evidence_packet.md)）
- **输出**：检索结果与证据引用列表

---

## 禁用/降级策略

- SeekDB 不可用时，Gateway 仍可基于 Logbook 进行最小化证据回溯
- 禁用开关由 Logbook/治理设置统一控制

---

更新时间：2026-01-31
