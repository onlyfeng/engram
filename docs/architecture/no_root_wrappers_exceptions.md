# 根目录 Wrapper 例外管理规范

> **状态**：当前  
> **更新日期**：2026-02-01  
> **关联文档**：[cli_entrypoints.md](cli_entrypoints.md)（主 CLI 入口文档）

---

## SSOT（Single Source of Truth）声明

| 资源 | 权威来源 | 说明 |
|------|----------|------|
| **Allowlist 数据模型** | `schemas/no_root_wrappers_allowlist_v1.schema.json` | JSON Schema 定义，字段约束与格式验证 |
| **Allowlist 数据文件** | `scripts/ci/no_root_wrappers_allowlist.json` | CI 门禁检查的例外允许列表 |
| **检查脚本** | `scripts/ci/check_no_root_wrappers_usage.py` | 执行例外检查的 CI 脚本 |
| **规范文档** | 本文档 | 语义定义、变更流程、治理策略 |

> **维护规则**：Schema 定义数据格式，本文档定义语义和流程。新增或修改例外时，须同时更新 allowlist 数据文件和相关文档。

### 字段兼容性说明

> **重要**：新增条目请使用 Schema 定义的标准字段名。旧字段名仅为过渡兼容保留，将在未来版本废弃。

| 推荐字段（Schema 标准） | 旧字段（兼容支持） | 说明 |
|-------------------------|-------------------|------|
| `file_glob` | `file_pattern` | 文件 glob 匹配模式 |
| `file_path` | - | 精确文件路径（优先级高于 `file_glob`） |
| `expires_on` | `expiry` | 过期日期（YYYY-MM-DD） |
| `jira_ticket` | `ticket` | 关联工单号 |

检查脚本同时支持新旧字段，但**推荐使用 Schema 定义的标准字段名**以确保长期兼容性。

---

## 1. 概述

本文档定义根目录 wrapper 模块导入例外的完整管理规范，包括数据模型、过期语义、负责人规范、分类体系和变更流程。

### 1.1 背景

Engram 项目正在将根目录的 CLI wrapper 脚本迁移到 `src/engram/` 包结构。迁移期间，部分代码（主要是测试）仍需导入根目录 wrapper 以验证向后兼容性。CI 门禁检查会阻止新增的根目录 wrapper 导入，但可通过例外机制允许特定情况。

### 1.2 例外声明方式

支持两种互补的例外声明方式：

| 方式 | 权威文件 | 适用场景 | 管理粒度 |
|------|----------|----------|----------|
| **Allowlist（集中管理）** | `scripts/ci/no_root_wrappers_allowlist.json` | 持久例外、需要追踪的例外 | 按文件 + 模块 |
| **Inline Marker（行内声明）** | 源代码行尾注释 | 临时例外、单点例外 | 按行 |

### 1.3 Deprecated vs Preserved 的治理差异

**这是理解整个例外机制的核心前提**：并非所有根目录模块都受 CI 检查约束。

#### 模块分类定义

| 分类 | 定义位置 | CI 检查 | Allowlist 要求 | 移除时间表 |
|------|----------|---------|----------------|------------|
| **Deprecated 模块** | `import_migration_map.json` 中 `deprecated: true` | ✅ 检查 | 必须（带 expiry/owner） | 有（v2.0 等） |
| **Preserved 模块** | `import_migration_map.json` 中 `deprecated: false` | ❌ 跳过 | **不需要** | 无 |

#### 治理策略差异

**Deprecated 模块**（如 `artifact_cli`, `db_migrate`, `scm_sync_*` 等）：

- CI 门禁会扫描 `src/` 和 `tests/` 中的导入
- 未授权导入会导致 CI 失败
- 需通过 **Allowlist** 或 **Inline Marker** 申请临时豁免
- 豁免必须设置过期日期（`expires_on`）和负责人（`owner`）
- 过期后 CI 失败，强制清理或续期

**Preserved 模块**（如 `db`, `kv`, `artifacts`）：

- CI 门禁**完全跳过**这些模块
- 可在任何位置自由导入，无需任何声明
- 不设移除时间表，作为长期工具模块保留

#### 代码示例

```python
# ✅ Preserved 模块：直接导入即可
import db
import kv
from artifacts import get_artifact_path

# ⚠️ Deprecated 模块：必须有豁免声明
# 方式 1：引用 allowlist 中的条目 ID
import artifact_cli  # ROOT-WRAPPER-ALLOW: test-artifact-cli-v1

# 方式 2：inline 完整声明
import db_migrate  # ROOT-WRAPPER-ALLOW: 迁移测试; expires=2026-06-30; owner=@engram-team
```

#### 常见误区

| 误区 | 正确理解 |
|------|----------|
| "所有根目录模块都需要 allowlist" | ❌ 只有 deprecated 模块需要 |
| "db/kv/artifacts 需要在 allowlist 中" | ❌ 它们是 preserved，无需任何声明 |
| "Allowlist 条目可以没有过期日期" | ❌ deprecated 模块必须有 expires_on |
| "Inline marker 可以省略 owner" | ❌ 必须同时提供 expires 和 owner |

#### 权威数据来源

- **模块分类定义**：`configs/import_migration_map.json`
- **CI 检查逻辑**：`scripts/ci/check_no_root_wrappers_usage.py`

详细迁移映射参见 [no_root_wrappers_migration_map.md](no_root_wrappers_migration_map.md)。

---

## 2. Allowlist 数据模型

### 2.1 Schema 版本

当前版本：**1.0**

Schema 定义文件：`schemas/no_root_wrappers_allowlist_v1.schema.json`

> **版本兼容性**：检查脚本同时接受 `version: "1"` 和 `version: "1.0"`，两者语义等价。新文件推荐使用 `"1.0"`。

### 2.2 顶层结构

```json
{
  "version": "1.0",
  "generated_at": "2026-02-01T12:00:00Z",
  "description": "根目录 wrapper 导入例外允许列表",
  "entries": []
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `version` | string | 是 | Schema 版本，当前固定为 `"1.0"` |
| `generated_at` | ISO8601 datetime | 否 | 文件生成/更新时间戳 |
| `description` | string | 否 | 文件用途说明 |
| `entries` | array | 是 | 例外条目列表 |

### 2.3 条目结构 (allowlist_entry)

每个例外条目包含以下字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 唯一标识符，格式：`<scope>-<module>-<描述>` |
| `scope` | enum | 是 | 例外作用范围：`import` 或 `subprocess` |
| `module` | string | 是 | 被允许的根目录 wrapper 模块名（不带 `.py`） |
| `file_glob` | string | 是* | 适用的文件 glob 模式（**推荐**，替代旧字段 `file_pattern`） |
| `file_path` | string | 否 | 精确文件路径（优先级高于 `file_glob`） |
| `reason` | string | 是 | 允许例外的原因说明（≥10 字符） |
| `owner` | string | 是 | 负责人（GitHub handle 或团队） |
| `expires_on` | ISO8601 date | 否 | 过期日期（YYYY-MM-DD）（**推荐**，替代旧字段 `expiry`） |
| `category` | enum | 是 | 例外分类 |
| `created_at` | ISO8601 date | 否 | 条目创建日期 |
| `jira_ticket` | string | 否 | 关联工单号（**推荐**，替代旧字段 `ticket`） |
| `notes` | string | 否 | 补充说明 |

> **注意**：标注 `*` 的字段存在旧字段别名，详见上方"字段兼容性说明"。`file_glob`、`file_path`、`file_pattern` 三选一即可。

### 2.4 ID 命名规范

ID 格式建议：`<scope>-<module>-<描述>`

| 模式 | 示例 | 说明 |
|------|------|------|
| `import-<module>-<test-name>` | `import-db-scm-sync-integration` | 导入例外，用于特定测试 |
| `subprocess-<module>-<context>` | `subprocess-artifact_cli-ci-smoke` | 子进程调用例外 |

约束：
- 只允许小写字母、数字、下划线、连字符
- 必须全局唯一
- 应具有描述性，便于在报告中识别

### 2.5 完整示例

```json
{
  "version": "1.0",
  "generated_at": "2026-02-01T12:00:00Z",
  "description": "根目录 wrapper 导入例外允许列表",
  "entries": [
    {
      "id": "import-db-acceptance-test-scm-sync",
      "scope": "import",
      "module": "db",
      "file_glob": "tests/logbook/test_scm_sync_integration.py",
      "reason": "验收测试需要验证根目录 db.py 兼容包装器的行为，确保 scm_db 向后兼容",
      "owner": "@engram-team",
      "expires_on": "2026-12-31",
      "category": "acceptance_test",
      "created_at": "2026-02-01",
      "jira_ticket": "ENG-1234",
      "notes": "迁移完成后可移除此例外"
    }
  ]
}
```

---

## 3. 过期语义 (Expiry Semantics)

### 3.1 日期格式

| 上下文 | 格式 | 示例 | 说明 |
|--------|------|------|------|
| Allowlist `expires_on` | ISO8601 日期 | `2026-12-31` | 仅日期，无时区 |
| Allowlist `created_at` | ISO8601 日期 | `2026-02-01` | 仅日期 |
| Allowlist `generated_at` | ISO8601 日期时间 | `2026-02-01T12:00:00Z` | 完整时间戳，带时区 |
| Inline Marker `expires=` | ISO8601 日期 | `expires=2026-06-30` | 仅日期 |

### 3.2 时区处理

| 规则 | 说明 |
|------|------|
| **存储** | 日期字段不含时区信息，仅存储 YYYY-MM-DD |
| **解释** | 统一按 **UTC 时区** 解释过期日期 |
| **过期时刻** | 过期日期当天 **UTC 23:59:59** 之后视为过期 |
| **CI 检查时机** | CI 运行时使用 `datetime.now(timezone.utc).date()` 获取当前 UTC 日期 |

**实现说明**：

所有 CI 门禁脚本使用共享的日期工具模块 `scripts/ci/_date_utils.py`：

```python
from _date_utils import utc_today

# 获取当前 UTC 日期
today = utc_today()  # 等价于 datetime.now(timezone.utc).date()

# 过期判定逻辑
is_expired = today > expires_date  # today == expires_date 仍有效
```

核心判定函数支持 `today` 参数注入，便于单元测试：

```python
# InlineMarker.is_expired(today=...)
# AllowlistEntry.is_expired(today=...)
# DepsDbAllowMarker.is_expired(today=...)
# validate_expires_on(..., today=...)
```

### 3.3 过期阈值与行为

| 状态 | 条件 | CI 行为 | 说明 |
|------|------|---------|------|
| **有效** | `expires_on` 未设置或 `today <= expires_on` | 允许 | 例外生效 |
| **即将过期** | `0 <= (expires_on - today) <= 14天` | 警告 | 输出提醒，建议续期或迁移 |
| **超过最大期限** | `(expires_on - today) > 180天` | 警告/报错 | 默认警告，可配置为报错 |
| **已过期** | `today > expires_on` | 报错 | 条目被忽略，导入视为违规 |

**预警阈值说明**：

| 阈值 | 默认值 | CLI 参数 | 说明 |
|------|--------|----------|------|
| 即将过期（expiring-soon） | 14 天 | `--expiring-soon-days` | 在 expires_on 前 14 天开始警告 |
| 最大期限（max-expiry） | 180 天 | `--max-expiry-days` | 超过 6 个月的过期日期需要审批 |
| 超期限失败 | 否 | `--fail-on-max-expiry` | 启用后，超过最大期限会导致 CI 失败 |

**CI 预警输出示例**：

```
[WARN] 即将过期的条目（14 天内）: 2 个
  id: test-legacy-import (3 天后过期, owner: @platform-team)
  id: acceptance-test-db (10 天后过期, owner: @qa-team)

[WARN] 超过最大期限（180 天）的条目: 1 个
  id: long-term-migration (距离过期 365 天, owner: @infra-team)

[汇总] 分类统计:
  acceptance_test: 5 个
  migration: 3 个
  testing: 2 个

[汇总] 负责人统计:
  @platform-team: 4 个
  @qa-team: 3 个
  @infra-team: 3 个
```

### 3.4 Inline Marker 过期处理

```python
# 格式
import db  # ROOT-WRAPPER-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>
```

| 状态 | CI 行为 |
|------|---------|
| 有效 | 允许导入 |
| 已过期 | **CI 失败**（与 Allowlist 不同，过期的 inline marker 直接报错） |
| 格式错误 | 报告警告，导入视为违规 |

> **重要**：Inline marker 过期后导致 CI 失败，负责人需在过期前完成迁移或更新过期日期。

### 3.5 续期规则

| 规则 | 说明 |
|------|------|
| **续期上限** | 单次续期不超过 **6 个月** |
| **累计上限** | 同一例外累计有效期不超过 **2 年**（需升级为永久例外或完成迁移） |
| **续期审批** | 续期需在 PR 中说明理由，建议关联工单 |

### 3.6 审批要求与治理策略

新增或续期例外时，根据期限长度需要不同级别的审批：

| 期限类型 | 期限范围 | 审批要求 | 说明 |
|----------|----------|----------|------|
| 短期例外 | ≤ 90 天 | 团队 Lead 审批 | 临时迁移过渡，无需额外理由 |
| 中期例外 | 91-180 天 | Tech Lead 审批 | 需要提供明确的迁移计划 |
| 长期例外 | > 180 天 | 架构组审批 | 需要 ADR 记录原因和迁移策略 |

**审批流程**：

1. **短期例外**：PR 需要至少一位团队 Lead 的 Approve
2. **中期例外**：PR 需要 Tech Lead Approve + 迁移计划文档链接
3. **长期例外**：
   - 需要架构组 Approve
   - 需要关联 ADR 文档
   - PR 描述中说明无法在 6 个月内完成迁移的原因

**回滚方式**（CI 失败时的紧急处理）：

如果例外条目或 inline marker 过期导致 CI 失败，可通过以下方式临时绕过：

1. **更新过期日期**：直接修改 allowlist JSON 或 inline marker 中的过期日期
2. **禁用门禁**（仅紧急情况）：设置环境变量 `SKIP_NO_ROOT_WRAPPERS_CHECK=1`

> **注意**：紧急绕过后必须在下一个工作日内创建跟踪 issue 并恢复门禁。

---

## 4. Owner 规范

### 4.1 格式要求

| 格式 | 示例 | 说明 |
|------|------|------|
| **GitHub 用户名** | `@alice` | 个人负责 |
| **GitHub 团队** | `@engram-team` | 团队负责（推荐） |
| **多负责人** | `@alice,@bob` | 逗号分隔（不推荐，应使用团队） |

### 4.2 负责人职责

| 职责 | 说明 |
|------|------|
| **监控过期** | 在例外过期前完成迁移或续期 |
| **审批变更** | 作为 PR reviewer 审批对该例外的修改 |
| **响应查询** | 回答关于该例外的技术问题 |
| **迁移执行** | 负责将代码迁移到新入口，移除例外 |

### 4.3 负责人变更

负责人变更需通过 PR，并在 PR 描述中说明变更原因和新负责人的确认。

---

## 5. 例外分类 (Category)

### 5.1 分类枚举

| 分类 | 代码值 | 说明 | 典型场景 |
|------|--------|------|----------|
| **验收测试** | `acceptance_test` | 验收测试需要验证兼容行为 | 测试根目录 wrapper 的向后兼容性 |
| **集成测试** | `integration_test` | 集成测试的特殊需求 | 测试跨模块交互 |
| **兼容包装** | `compat_wrapper` | 兼容层本身的实现 | wrapper 模块内部的自引用 |
| **遗留迁移** | `legacy_migration` | 迁移过程中的临时例外 | 渐进式迁移的中间状态 |
| **工具脚本** | `tooling` | CI/运维工具的特殊需求 | 脚本需要调用根目录入口 |
| **其他** | `other` | 不属于以上类别 | 需在 `notes` 中详细说明 |

### 5.2 分类指导

| 场景 | 推荐分类 | 是否需要 `expires_on` |
|------|----------|----------------------|
| 测试验证 wrapper 行为 | `acceptance_test` | 是 |
| 迁移进行中的临时代码 | `legacy_migration` | 是（必须） |
| wrapper 模块的单元测试 | `compat_wrapper` | 否（随 wrapper 移除） |
| CI 脚本调用旧入口 | `tooling` | 是 |
| 第三方工具限制 | `other` | 是 |

### 5.3 分类统计

CI 检查脚本会按分类统计例外数量，便于治理决策：

```
[INFO] 例外统计:
  - acceptance_test: 3 条
  - integration_test: 1 条
  - legacy_migration: 2 条（含 1 条即将过期）
  - 总计: 6 条
```

---

## 6. 变更流程

### 6.1 新增例外流程

```
┌─────────────────────────────────────────────────────────────────┐
│                     新增例外流程                                  │
├─────────────────────────────────────────────────────────────────┤
│  1. 创建 Issue/工单                                              │
│     - 描述为何需要例外                                            │
│     - 说明迁移计划和时间表                                        │
│     - 指定负责人                                                  │
│                                                                 │
│  2. 准备 PR                                                     │
│     - 更新 allowlist.json 添加条目                               │
│     - 或添加 inline marker                                       │
│     - 确保设置合理的 expires_on                                  │
│                                                                 │
│  3. PR 审批                                                     │
│     - 至少一位 CODEOWNER 审批                                    │
│     - 检查 expiry 是否合理（不超过 6 个月）                       │
│     - 检查 reason 是否充分                                       │
│                                                                 │
│  4. 合并后                                                       │
│     - CI 自动验证 allowlist schema                               │
│     - 例外立即生效                                               │
└─────────────────────────────────────────────────────────────────┘
```

### 6.2 续期流程

```
┌─────────────────────────────────────────────────────────────────┐
│                       续期流程                                    │
├─────────────────────────────────────────────────────────────────┤
│  1. 评估迁移进度                                                  │
│     - 检查迁移是否可完成                                          │
│     - 评估剩余工作量                                              │
│                                                                 │
│  2. 更新工单                                                     │
│     - 说明续期原因                                               │
│     - 更新迁移时间表                                              │
│                                                                 │
│  3. 提交 PR                                                     │
│     - 更新 expires_on（不超过 6 个月）                           │
│     - 在 notes 中记录续期历史                                    │
│                                                                 │
│  4. 审批（同新增）                                               │
└─────────────────────────────────────────────────────────────────┘
```

### 6.3 移除例外流程

```
┌─────────────────────────────────────────────────────────────────┐
│                     移除例外流程                                  │
├─────────────────────────────────────────────────────────────────┤
│  1. 完成迁移                                                     │
│     - 代码已迁移到新入口                                          │
│     - 测试通过                                                   │
│                                                                 │
│  2. 提交 PR                                                     │
│     - 删除 allowlist 条目或 inline marker                        │
│     - 确保 CI 通过（无新的违规）                                  │
│                                                                 │
│  3. 关闭关联工单                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 6.4 审批要求

| 变更类型 | 审批人 | 额外要求 |
|----------|--------|----------|
| 新增例外 | 至少 1 位 CODEOWNER | 需关联 Issue |
| 续期 | 至少 1 位 CODEOWNER | 需说明续期原因 |
| 移除 | 无特殊要求 | 确保 CI 通过 |
| 修改 owner | 新旧 owner 确认 | 在 PR 中 @ 双方 |

---

## 7. Inline Marker 迁移策略

### 7.1 当前状态

截至 2026-02-01，代码库中存在使用 `# ROOT-WRAPPER-ALLOW:` inline marker 的位置。

### 7.2 迁移时间表

| 阶段 | 目标日期 | 行动 |
|------|----------|------|
| **阶段 1: 审计** | 2026-02-28 | 统计所有 inline marker，评估迁移难度 |
| **阶段 2: 集中化** | 2026-03-31 | 将持久例外迁移到 allowlist.json |
| **阶段 3: 精简** | 2026-06-30 | inline marker 仅保留临时例外（≤3 个月） |
| **阶段 4: 强制** | 2026-09-30 | CI 对超过 3 个月的 inline marker 报错 |

### 7.3 迁移规则

| inline marker 类型 | 迁移目标 |
|--------------------|----------|
| 持久例外（测试验证兼容性） | 迁移到 allowlist.json |
| 临时例外（迁移过渡） | 保留 inline marker，设置短过期 |
| 无 expiry 的旧 marker | 必须添加 expiry 或迁移 |

### 7.4 格式统一

所有 inline marker 必须遵循统一格式：

```python
# 行尾声明（推荐）
import db  # ROOT-WRAPPER-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>

# 上一行声明
# ROOT-WRAPPER-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>
import db
```

**禁止的格式**（将在 2026-03-31 后 CI 报错）：

```python
# 缺少 expiry
import db  # ROOT-WRAPPER-ALLOW: 测试需要

# 缺少 owner
import db  # ROOT-WRAPPER-ALLOW: 测试需要; expires=2026-06-30

# 格式错误
import db  # ALLOW-ROOT-WRAPPER: 测试需要
```

---

## 8. 治理指标

### 8.1 健康指标

| 指标 | 健康阈值 | 预警阈值 | 说明 |
|------|----------|----------|------|
| 总例外数 | ≤ 20 | > 30 | 过多例外说明迁移进度滞后 |
| 即将过期（14 天内） | ≤ 3 | > 5 | 需要立即处理 |
| 已过期 | 0 | > 0 | 应为 0，否则 CI 失败 |
| legacy_migration 类别占比 | ≤ 30% | > 50% | 迁移例外应逐步减少 |
| 平均剩余有效期 | ≥ 30 天 | < 14 天 | 例外管理健康度 |

### 8.2 定期审计

| 频率 | 审计内容 | 输出 |
|------|----------|------|
| 每周 | CI 自动统计例外数量和过期状态 | CI 日志 |
| 每月 | 人工审查即将过期的例外 | 更新或迁移 |
| 每季度 | 全量审计，评估迁移进度 | 治理报告 |

---

## 9. 附录

### 9.1 Schema 定义参考

完整 Schema 定义见：`schemas/no_root_wrappers_allowlist_v1.schema.json`

### 9.2 检查脚本参考

检查脚本位置：`scripts/ci/check_no_root_wrappers_usage.py`

常用选项：

```bash
# 运行检查
python scripts/ci/check_no_root_wrappers_usage.py --verbose

# 仅统计，不报错
python scripts/ci/check_no_root_wrappers_usage.py --stats-only

# 显示即将过期的例外
python scripts/ci/check_no_root_wrappers_usage.py --show-expiring
```

### 9.3 相关文档

| 文档 | 说明 |
|------|------|
| [cli_entrypoints.md](cli_entrypoints.md) | CLI 入口清单与调用规范 |
| [iteration_2_plan.md](iteration_2_plan.md) | 迁移计划 |

---

更新时间：2026-02-01（初始版本：定义 allowlist 数据模型、expiry 语义、owner 规范、分类体系、变更流程）
