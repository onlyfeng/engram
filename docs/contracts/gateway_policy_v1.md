# Gateway Policy V1 Contract

> 本文档定义 Gateway `policy_json` 字段的完整 Schema、默认值、取值域与向后兼容策略。

## 版本信息

| 项目 | 值 |
|------|-----|
| 版本 | v1 |
| 状态 | stable |
| 生效日期 | 2024-01-15 |

## 概述

`policy_json` 是 Gateway 治理策略的核心配置，存储于 `governance.settings.policy_json` 字段（JSONB 类型）。它控制以下行为：

- 团队空间写入权限控制
- 知识类型限制
- 证据链验证策略
- 内容长度限制
- 批量提交策略

## 完整 Schema

```json
{
  "allowlist_users": [],
  "allowed_kinds": ["PROCEDURE", "REVIEW_GUIDE", "PITFALL", "DECISION"],
  "require_evidence": true,
  "evidence_mode": "compat",
  "max_chars": 1200,
  "bulk_mode": "very_short",
  "bulk_max_chars": 200
}
```

## 字段定义

### allowlist_users

| 属性 | 值 |
|------|-----|
| 类型 | `List[str]` |
| 默认值 | `[]` |
| 必填 | 否 |

允许直接写入团队空间的用户列表。空列表表示**不限制用户**（所有用户均可写入，仍需通过其他策略检查）。

**行为说明**:
- 当列表为空时，跳过用户白名单检查
- 当列表非空时，仅允许列表中的用户写入团队空间
- 不在白名单中的用户写入请求会被重定向到 `private:<user>` 空间

**示例**:
```json
{
  "allowlist_users": ["alice", "bob", "team-lead"]
}
```

### allowed_kinds

| 属性 | 值 |
|------|-----|
| 类型 | `List[str]` |
| 默认值 | `["PROCEDURE", "REVIEW_GUIDE", "PITFALL", "DECISION"]` |
| 必填 | 否 |
| 允许值 | `FACT`, `PROCEDURE`, `PITFALL`, `DECISION`, `REVIEW_GUIDE` |

允许写入团队空间的知识类型列表。空列表表示**不限制类型**。

**行为说明**:
- 当列表为空时，跳过知识类型检查
- 当列表非空时，仅允许列表中的类型写入
- 不在列表中的类型会触发 `kind_not_allowed:<kind>` 原因的重定向

**示例**:
```json
{
  "allowed_kinds": ["PROCEDURE", "PITFALL"]
}
```

### require_evidence

| 属性 | 值 |
|------|-----|
| 类型 | `bool` |
| 默认值 | `true` |
| 必填 | 否 |

是否强制要求证据链引用。

**行为说明**:
- `true`: 写入请求必须包含非空的 `evidence_refs` 或 `evidence`
- `false`: 允许无证据链的写入
- 证据缺失会触发 `missing_evidence` 原因的重定向

**注意**: 此字段仅检查证据是否存在，不校验证据内容的有效性。证据内容校验由 `evidence_mode` 控制。

### evidence_mode

| 属性 | 值 |
|------|-----|
| 类型 | `str` |
| 默认值 | `"compat"` |
| 必填 | 否 |
| 允许值 | `compat`, `strict` |

证据验证模式，控制对 `evidence` (v2) 字段的校验强度。

**取值说明**:

| 模式 | 说明 | sha256 校验 | 向后兼容 |
|------|------|-------------|----------|
| `compat` | 兼容模式（默认） | 不强制 | 是 |
| `strict` | 严格模式 | 强制要求 | 否 |

**compat 模式行为**:
- 接受 v1 格式 (`evidence_refs: List[str]`) 和 v2 格式 (`evidence: List[object]`)
- v1 格式自动映射为 v2 external 格式（sha256 为空）
- 不校验 sha256 是否存在
- 记录 compat_warnings 用于可观测性

**strict 模式行为**:
- 仅接受 v2 格式 (`evidence`)
- 要求每个 evidence 对象必须包含有效的 `sha256` 字段
- sha256 缺失触发 `evidence_sha256_missing` 校验失败
- 受 `STRICT_MODE_ENFORCE_VALIDATE_REFS` 环境变量控制

**与环境变量的关系**:

| 配置 | 优先级 | 说明 |
|------|--------|------|
| `evidence_mode` (policy) | 高 | 策略级配置，推荐使用 |
| `VALIDATE_EVIDENCE_REFS` (env) | 中 | 环境变量，全局默认 |
| `STRICT_MODE_ENFORCE_VALIDATE_REFS` (env) | - | 控制 strict 模式是否强制启用校验 |

**决策逻辑** (实现于 `config.resolve_validate_refs`):
1. `strict` 模式 + `STRICT_MODE_ENFORCE_VALIDATE_REFS=true`: 强制启用校验
2. `strict` 模式 + `STRICT_MODE_ENFORCE_VALIDATE_REFS=false`: 允许环境变量覆盖
3. `compat` 模式: 使用环境变量 `VALIDATE_EVIDENCE_REFS` 的值

### max_chars

| 属性 | 值 |
|------|-----|
| 类型 | `int` |
| 默认值 | `1200` |
| 必填 | 否 |
| 最小值 | `1` |

单次写入的最大字符数限制。

**行为说明**:
- 超过限制触发 `exceeds_max_chars:<actual>><limit>` 原因的重定向
- 字符数计算基于 `payload_md` 的 Python `len()` 函数（UTF-8 字符计数）

### bulk_mode

| 属性 | 值 |
|------|-----|
| 类型 | `str` |
| 默认值 | `"very_short"` |
| 必填 | 否 |
| 允许值 | `very_short`, `reject`, `allow` |

批量提交 (`is_bulk=true`) 的处理策略。

**取值说明**:

| 模式 | 说明 | 行为 |
|------|------|------|
| `very_short` | 限制短内容 | 批量提交内容超过 `bulk_max_chars` 时重定向 |
| `reject` | 禁止批量 | 任何批量提交都重定向 |
| `allow` | 完全允许 | 批量提交不受额外限制 |

**reason 对应**:
- `very_short` 模式超限: `bulk_too_long`
- `reject` 模式: `bulk_not_allowed`

### bulk_max_chars

| 属性 | 值 |
|------|-----|
| 类型 | `int` |
| 默认值 | `200` |
| 必填 | 否 |
| 最小值 | `1` |

`bulk_mode=very_short` 时的最大字符数限制。

**注意**: 此字段仅在 `bulk_mode=very_short` 时生效。

## 默认策略

未配置 `policy_json` 时使用的默认策略：

```python
DEFAULT_POLICY = {
    "allowlist_users": [],           # 不限制用户
    "allowed_kinds": ["PROCEDURE", "REVIEW_GUIDE", "PITFALL", "DECISION"],
    "require_evidence": True,        # 要求证据链
    "evidence_mode": "compat",       # 兼容模式
    "max_chars": 1200,               # 最大 1200 字符
    "bulk_mode": "very_short",       # 批量提交限 200 字符
    "bulk_max_chars": 200,           # bulk 模式字符限制
}
```

## 策略决策 Reason 码

所有策略决策都会返回 `reason` 字段，用于审计和调试：

| reason | 含义 | 触发条件 |
|--------|------|----------|
| `private_space` | 私有空间 | 目标空间为 `private:*` |
| `team_write_disabled` | 团队写入关闭 | `team_write_enabled=false` |
| `user_not_in_allowlist` | 用户不在白名单 | 用户不在 `allowlist_users` 列表 |
| `kind_not_allowed:<kind>` | 类型不允许 | 知识类型不在 `allowed_kinds` 列表 |
| `missing_evidence` | 缺少证据 | `require_evidence=true` 且无证据 |
| `exceeds_max_chars:<actual>><limit>` | 超过字符限制 | 内容长度超过 `max_chars` |
| `bulk_too_long` | 批量内容过长 | `bulk_mode=very_short` 且超过 `bulk_max_chars` |
| `bulk_not_allowed` | 批量不允许 | `bulk_mode=reject` |
| `unknown_space_type` | 未知空间类型 | 目标空间不是 `private:`/`team:`/`org:` |
| `policy_passed` | 策略通过 | 所有检查通过 |

### Evidence 验证相关 Reason 码

在 `evidence_mode=strict` 时可能出现的额外 reason 码：

| reason | 含义 | 触发条件 |
|--------|------|----------|
| `evidence_sha256_missing` | 缺少 SHA256 | strict 模式下 evidence.sha256 为空 |
| `evidence_format_invalid` | 格式无效 | evidence 对象结构不符合规范 |

## 向后兼容策略

### 新增字段兼容性

| 字段 | 引入版本 | 向后兼容 | 迁移方式 |
|------|----------|----------|----------|
| `allowlist_users` | v1 | - | 原始字段 |
| `allowed_kinds` | v1 | - | 原始字段 |
| `require_evidence` | v1 | - | 原始字段 |
| `max_chars` | v1 | - | 原始字段 |
| `bulk_mode` | v1 | - | 原始字段 |
| `evidence_mode` | v1.1 | 是 | 默认 `compat`，无需迁移 |
| `bulk_max_chars` | v1.1 | 是 | 默认 `200`，无需迁移 |

### 缺失字段处理

- 所有字段都有默认值
- 缺失的字段自动使用默认值
- 不会因缺失字段导致策略评估失败

### 升级路径

**从无策略升级到 v1**:
```sql
-- 创建初始策略
UPDATE governance.settings 
SET policy_json = '{
  "allowlist_users": [],
  "allowed_kinds": ["PROCEDURE", "REVIEW_GUIDE", "PITFALL", "DECISION"],
  "require_evidence": true
}'::jsonb
WHERE project_key = 'myproject';
```

**从 compat 升级到 strict**:
1. 确保所有 evidence 都包含 sha256
2. 运行迁移检查脚本验证数据
3. 更新 policy_json:
```sql
UPDATE governance.settings 
SET policy_json = policy_json || '{"evidence_mode": "strict"}'::jsonb
WHERE project_key = 'myproject';
```

## 配置与策略的职责划分

| 配置项 | 配置来源 | 职责 |
|--------|----------|------|
| `team_write_enabled` | `governance.settings` | 团队写入总开关（独立于 policy） |
| `policy_json.*` | `governance.settings.policy_json` | 策略细节配置 |
| `VALIDATE_EVIDENCE_REFS` | 环境变量 | 全局 evidence 校验开关（默认值） |
| `STRICT_MODE_ENFORCE_VALIDATE_REFS` | 环境变量 | strict 模式强制校验开关 |
| `unknown_actor_policy` | 环境变量 | 未知用户处理策略 |

**职责说明**:
- **policy_json**: 业务策略，由 TL 或管理员通过 `governance_update` 接口调整
- **环境变量**: 运维配置，部署时确定，通常不频繁变更
- **团队开关**: 高级别开关，控制是否启用团队空间写入

## 审计日志

所有策略决策都会写入 `governance.write_audit` 表：

```sql
-- 查询策略拦截记录
SELECT 
    created_at,
    actor_user_id,
    target_space,
    action,
    reason,
    evidence_refs_json->'gateway_event'->>'policy_reason' as policy_reason
FROM governance.write_audit
WHERE reason LIKE 'POLICY_%'
ORDER BY created_at DESC
LIMIT 100;
```

## 相关文档

- [团队可写开关与策略治理](../gateway/04_governance_switch.md)
- [Evidence Packet 契约](./evidence_packet.md)
- [Gateway-Logbook 边界](./gateway_logbook_boundary.md)
