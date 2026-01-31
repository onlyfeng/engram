# Schema 版本控制契约

本文档定义 JSON Schema 及其他契约的版本控制规则、变更流程与测试要求。

## 概述

所有 `schemas/*.schema.json` 文件定义了系统中关键数据结构的契约，任何变更都必须通过契约测试验证。

---

## 受管 Schema 列表

| Schema 文件 | 用途 | 契约测试 |
|------------|------|----------|
| [`audit_event_v1.schema.json`](../../schemas/audit_event_v1.schema.json) | Gateway 审计事件格式 | `test_audit_event_contract.py` |
| `object_store_audit_event_v1.schema.json` | 对象存储审计事件归一化格式 | `test_audit_event_contract.py` |
| [`reliability_report_v1.schema.json`](../../schemas/reliability_report_v1.schema.json) | 可靠性报告格式 | `test_reliability_report_contract.py` |

---

## 契约类型

### 1. JSON Schema 契约

**文件命名规范**：`*_v{major}.schema.json`

- **文件名中的 major 版本号固定**，仅在破坏性变更时递增
- **Schema 内部 `schema_version` 字段**允许 `1.x` 演进（如 `1.0` → `1.1` → `1.2`）

**版本演进规则**：

| 变更类型 | 版本影响 | 示例 |
|----------|---------|------|
| 新增可选字段 | minor（1.0 → 1.1） | 添加 `policy` 子结构 |
| 放宽约束 | minor（1.1 → 1.2） | `maxItems: 5` → `maxItems: 10` |
| 新增枚举值 | minor | `source` 枚举增加 `reconcile_outbox` |
| 删除字段 | **major**（v1 → v2） | 移除 `legacy_field` |
| 修改语义 | **major** | `refs` 改为必须包含 SHA256 |
| 收紧约束 | **major** | `maxItems: 10` → `maxItems: 5` |

### 2. 非 Schema 契约

对于行为契约、协议约定等非 JSON Schema 的契约文档，采用以下规范：

**文件命名**：`docs/contracts/<contract_name>_v<major>.md`

**示例**：
- `docs/contracts/outbox_lease_v1.md` — Outbox Worker 租约机制
- `docs/contracts/evidence_packet_v1.md` — Evidence Packet 结构

**版本升级规则**：
- 重大行为变化（如租约超时语义改变、协议握手流程变更）→ 新开 v2 文档
- 文档补充、澄清、非破坏性细节 → 原文档内更新

---

## 变更规则

### 必须通过契约测试

**任何 schema 变更都必须通过对应的契约测试。** 契约测试位于：

- `tests/gateway/test_audit_event_contract.py`
- `tests/gateway/test_reliability_report_contract.py`

这些测试使用 `jsonschema` 库（Draft 2020-12）验证：

1. Schema 语法正确性
2. 示例数据（`examples`）符合 schema
3. 必需字段存在且类型正确
4. 枚举值、格式约束生效
5. 边界条件（如 `minimum`、`maximum`、`maxItems`）正确执行

### 向后兼容变更（Minor，无需迁移）

| 变更类型 | 示例 | 测试要求 |
|----------|------|----------|
| 新增可选字段 | 添加 `metadata` 对象 | 更新测试覆盖新字段 |
| 放宽约束 | `maxItems: 5` → `maxItems: 10` | 更新边界测试 |
| 新增枚举值 | `source` 枚举增加新来源 | 添加新值的测试用例 |
| 扩展 `oneOf`/`anyOf` | 支持新的子结构 | 添加新结构的测试 |

### 破坏性变更（Major，需迁移计划）

| 变更类型 | 影响 | 处理方式 |
|----------|------|----------|
| 移除必需字段 | 现有数据校验失败 | 先标记 deprecated，下一版本移除 |
| 修改字段类型 | 序列化/反序列化失败 | 版本号升级（如 v1 → v2） |
| 收紧约束 | 现有有效数据变为无效 | 数据迁移 + 版本升级 |
| 重命名字段 | 消费方代码失效 | 版本号升级 + 兼容层 |
| 改变语义 | 消费方逻辑错误 | 版本号升级 + 迁移指南 |

---

## 版本号规范

Schema 文件名包含版本号：`<name>_v<major>.schema.json`

- **major 版本**（文件名）：破坏性变更时递增
- **minor 版本**（`schema_version` 字段）：向后兼容变更时递增

### 版本升级示例

```
# 当前版本（向后兼容演进）
audit_event_v1.schema.json          # 文件名不变
  └── schema_version: "1.0" → "1.1" → "1.2"  # 内部版本递增

# 破坏性变更后
audit_event_v2.schema.json  # 新文件，schema_version: "2.0"
audit_event_v1.schema.json  # 保留旧版本，标记 deprecated
```

---

## CI/CD 要求

### 测试依赖

契约测试依赖 `jsonschema>=4.18.0`（支持 Draft 2020-12），已包含在 Gateway 的开发依赖中：

```toml
# pyproject.toml
[project.optional-dependencies]
dev = [
    ...
    "jsonschema>=4.18.0",  # Draft 2020-12 support for contract tests
]
```

### 强制失败策略

契约测试 **不使用** `skipif` 条件跳过。缺少 `jsonschema` 依赖将导致测试直接失败（`ImportError`），确保 CI 环境正确配置。

### 运行方式

```bash
# 安装开发依赖（包含 jsonschema）
pip install -e ".[dev]"

# 运行契约测试
pytest tests/gateway/test_audit_event_contract.py
pytest tests/gateway/test_reliability_report_contract.py
```

---

## 契约变更检查清单

**改动以下任一项时，必须同步更新相关资源：**

### Schema 变更

- [ ] 变更类型已识别（minor / major）
- [ ] `schema_version` 字段已更新（minor）或文件名已升级（major）
- [ ] 契约测试已更新，覆盖新字段/约束
- [ ] `examples` 数组已更新为有效示例
- [ ] `schemas/fixtures/` 下的测试 fixtures 已同步更新
- [ ] 本地 `pytest` 通过
- [ ] 如为破坏性变更：
  - [ ] 版本号已升级（文件名 v1 → v2）
  - [ ] 迁移计划已记录
  - [ ] 旧版本标记 deprecated

### URI 格式变更

（如 `memory://patch_blobs/...`、`memory://attachments/...` 等）

- [ ] 相关 Schema 中的 URI pattern 已更新
- [ ] URI 解析/生成代码已同步修改
- [ ] 单元测试中的 URI 用例已更新
- [ ] `schemas/fixtures/` 下的 fixture 文件已更新
- [ ] 文档中的 URI 示例已更新

### Outbox 行为变更

（如租约机制、重试策略、状态转换等）

- [ ] `docs/contracts/` 下的相关契约文档已更新
- [ ] 如为重大行为变化，已新建 v2 文档
- [ ] Outbox Worker 代码与文档一致
- [ ] 相关集成测试已更新

### Degradation 降级行为变更

（如 `LOGBOOK_DOWN`、`OPENMEMORY_DOWN` 降级路径）

- [ ] `docs/gateway/05_failure_degradation.md` 已更新
- [ ] 降级相关的审计事件 reason 枚举已在 schema 中声明
- [ ] 降级路径的集成测试已覆盖
- [ ] 告警/监控文档已同步更新（如适用）

---

---

## 破坏性变更流程

### 必须执行的步骤

当变更被识别为**破坏性变更**（Major）时，必须按以下流程执行：

#### 1. 迁移脚本

在 `sql/` 目录新增迁移脚本：

```sql
-- Migration: YYYYMMDD_HHMMSS_<migration_name>.sql
-- Description: 描述变更内容

-- UP: 应用迁移
ALTER TABLE logbook.some_table ADD COLUMN new_field TEXT;

-- DOWN: 回滚迁移
ALTER TABLE logbook.some_table DROP COLUMN new_field;
```

#### 2. Backfill/Repair 命令

对于影响现有数据的变更，必须提供修复工具：

| 工具类型 | 文件路径 | 要求 |
|----------|----------|------|
| Backfill 脚本 | `backfill_<feature>.py`（放在 `logbook_postgres/scripts/`） | 支持 `--dry-run`、`--batch-size`、断点续传 |
| Repair 脚本 | `repair_<issue>.py`（放在 `logbook_postgres/scripts/`） | 支持 `--dry-run`、输出统计 |

#### 3. 版本判定更新

| 更新位置 | 内容 |
|----------|------|
| Schema 文件名 | `v1` → `v2` |
| Schema 内 `schema_version` | `2.0`（重置为 major.0） |
| 旧 Schema 文件 | 添加 `deprecated: true` |
| `CHANGELOG.md` | 记录破坏性变更 |
| 本文档（附录） | 更新变更历史 |

### 破坏性变更检查清单

- [ ] 变更类型已识别为 Major（破坏性）
- [ ] 迁移脚本已添加（包含 UP/DOWN）
- [ ] Backfill/Repair 命令已提供（如影响现有数据）
- [ ] 文件名版本已升级（v1 → v2）
- [ ] 旧版本已标记 deprecated
- [ ] 迁移指南已编写（如需用户手动操作）
- [ ] CHANGELOG.md 已更新
- [ ] 相关组件（Gateway/SeekDB）已同步适配

---

## 相关文档

| 主题 | 文档路径 |
|------|----------|
| Gateway ↔ Logbook 边界契约 | [docs/contracts/gateway_logbook_boundary.md](./gateway_logbook_boundary.md) |
| Evidence Packet 格式 | [docs/contracts/evidence_packet.md](./evidence_packet.md) |
| Memory Card 格式 | [docs/gateway/03_memory_contract.md](../gateway/03_memory_contract.md) |
| **Logbook DoD** | [docs/logbook/05_definition_of_done.md](../logbook/05_definition_of_done.md) |
