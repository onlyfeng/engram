# OpenMemory 文档目录

> **适用人群**：负责 OpenMemory 接入、升级与运维的开发者/运维

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [00_vendoring_and_patches.md](00_vendoring_and_patches.md) | 上游镜像、补丁分级、升级与回滚流程 |
| [01_upgrade_compatibility_matrix.md](01_upgrade_compatibility_matrix.md) | 已验证兼容面、版本锁定策略、升级前检查清单 |
| [02_ci_governance_contract.md](02_ci_governance_contract.md) | CI 治理与检查项约束 |

---

## 常见接入提醒

- 默认 `OM_USE_SUMMARY_ONLY=true` 时，OpenMemory 会先提炼摘要再落库；上层写入长记忆后，最终看到的内容可能只剩约 `200` 字。
- 如果上层需要保留完整记忆原文，请在部署配置中显式设置 `OM_USE_SUMMARY_ONLY=false`。
- 相关说明见 [`docs/reference/environment_variables.md`](../reference/environment_variables.md#openmemory-组件)；Windows / WSL2 排查示例见 [`docs/gateway/01_openmemory_deploy_windows.md`](../gateway/01_openmemory_deploy_windows.md)。

---

## 相关文档

- [Gateway 概览](../gateway/00_overview.md)
- [统一栈环境变量](../reference/environment_variables.md#openmemory-组件)
- [最小安全清单](../guides/security_minimal.md)

---

更新时间：2026-04-21
