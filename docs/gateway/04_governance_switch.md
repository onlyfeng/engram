# 团队可写开关（team_write_enabled）与策略治理

> **完整 policy_json Schema**: 参见 [Gateway Policy V1 Contract](../contracts/gateway_policy_v1.md)

## 开关含义
- team_write_enabled = true：允许写入 team:<project>（仍需通过策略校验）
- team_write_enabled = false：禁止直接写入 team 空间；写入请求会降级到 private 空间

## 策略（policy_json）Schema

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| allowlist_users | List[str] | [] | 允许直接写 team 的用户（TL/模块 owner） |
| allowed_kinds | List[str] | ["PROCEDURE", "REVIEW_GUIDE", "PITFALL", "DECISION"] | 允许写 team 的 Kind 列表 |
| require_evidence | bool | true | 是否强制证据链（存在性检查） |
| evidence_mode | str | "compat" | 证据验证模式: "compat" / "strict" |
| max_chars | int | 1200 | 最大字符数 |
| bulk_mode | str | "very_short" | 对 is_bulk 提交的处理模式 |
| bulk_max_chars | int | 200 | bulk_mode=very_short 时的字符限制 |

### bulk_mode 取值
- `very_short`: 批量提交限制 `bulk_max_chars` 字符内（默认 200）
- `reject`: 禁止批量提交
- `allow`: 允许任意长度批量提交

### evidence_mode 取值
- `compat`: 兼容模式（默认），接受 v1/v2 格式，不强制 sha256
- `strict`: 严格模式，要求 v2 格式且 sha256 必填

> 详细说明参见 [Gateway Policy V1 Contract](../contracts/gateway_policy_v1.md#evidence_mode)

## 便捷函数 decide_write

```python
from gateway.policy import decide_write

result = decide_write(
    actor_user_id="alice",
    requested_space="team:myproject",
    kind="PROCEDURE",
    is_bulk=False,
    payload_md="记忆内容...",
    evidence_refs=["commit:abc123"],
    settings=governance_settings,  # 或传入 policy_engine
)
# 返回: {"action": "allow"|"redirect"|"reject", "target_space": "...", "reason": "..."}
```

## 策略决策 reason 说明

| reason | 含义 |
|--------|------|
| private_space | 私有空间，直接允许 |
| team_write_disabled | 团队写入开关关闭 |
| user_not_in_allowlist | 用户不在白名单 |
| kind_not_allowed:{kind} | 知识类型不允许 |
| missing_evidence | 缺少证据链 |
| exceeds_max_chars:{actual}>{limit} | 超过最大字符数 |
| bulk_too_long | 批量提交内容过长（very_short 模式） |
| bulk_not_allowed | 批量提交不允许（reject 模式） |
| unknown_space_type | 未知空间类型 |
| policy_passed | 所有策略检查通过 |

## 审计
所有写入请求必须写 governance.write_audit：
- action：allow / redirect / reject
- reason：开关关闭 / 策略不满足 / 通过
- payload_sha：用于追溯与去重

## 治理设置更新接口

### REST 端点
```
POST /governance/settings/update
```

### MCP Tool
```
tool: governance_update
```

### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| team_write_enabled | bool | 否 | 是否启用团队写入 |
| policy_json | object | 否 | 策略 JSON（会与现有策略合并） |
| admin_key | string | 否* | 管理密钥（与 GOVERNANCE_ADMIN_KEY 环境变量匹配） |
| actor_user_id | string | 否* | 执行操作的用户标识（用于 allowlist 鉴权） |

*鉴权方式二选一：admin_key 或 actor_user_id（需在 allowlist_users 中）

### 鉴权方式

满足以下任一条件即可通过鉴权：

1. **admin_key 匹配**：请求中的 `admin_key` 与环境变量 `GOVERNANCE_ADMIN_KEY` 相同
2. **allowlist 用户**：`actor_user_id` 在当前 `policy_json.allowlist_users` 列表中

### 响应示例

```json
{
  "ok": true,
  "action": "allow",
  "settings": {
    "project_key": "myproject",
    "team_write_enabled": true,
    "policy_json": {
      "allowlist_users": ["alice", "bob"],
      "allowed_kinds": ["PROCEDURE", "REVIEW_GUIDE"]
    },
    "updated_by": "alice",
    "updated_at": "2024-01-15T10:30:00Z"
  },
  "message": null
}
```

### 审计日志

所有 governance_update 操作都会写入 governance.write_audit：
- target_space: `governance:<project_key>`
- action: `allow` 或 `reject`
- reason: `governance_update:<auth_method>` 或 `governance_update:<reject_reason>`

### reason 说明（governance_update）

| reason | 含义 |
|--------|------|
| governance_update:admin_key | 通过管理密钥鉴权 |
| governance_update:allowlist_user | 通过 allowlist 用户鉴权 |
| governance_update:missing_credentials | 未提供任何鉴权凭证 |
| governance_update:admin_key_not_configured | 服务端未配置 GOVERNANCE_ADMIN_KEY |
| governance_update:invalid_admin_key | 管理密钥不匹配 |
| governance_update:user_not_in_allowlist | 用户不在白名单中 |
| governance_update:internal_error | 内部错误 |

### 环境变量

| 变量名 | 说明 | 示例 |
|--------|------|------|
| GOVERNANCE_ADMIN_KEY | 治理管理密钥 | `sk-governance-xxx` |
