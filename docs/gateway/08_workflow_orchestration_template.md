# 项目工作流编排模板（基于 MCP 原语）

本文档提供**项目侧**的工作流编排模板，说明如何使用 Gateway 提供的 MCP 原语工具组合实现具体工程流程。
**注意**：此文档不定义新的 MCP 工具，仅约束“如何组合调用”。

---

## 适用范围

- 客户端/流水线希望**不直接访问数据库**，而通过 `engram-mcp` 统一执行能力
- 需要可追踪的资产化与记账（manifest.csv、svn-patch-merge 等）

参考原语契约：
- `docs/gateway/07_capability_boundary.md`
- `docs/logbook/02_tools_contract.md`

---

## 通用约定

### 追踪字段

| 字段 | 说明 |
|------|------|
| `correlation_id` | 每次请求统一追踪 ID（由 Gateway 生成并回传） |
| `project_key` | 项目标识（影响存储路径与资产归属） |
| `actor_user_id` | 操作者身份，用于审计与事件归属 |

### Logbook 命名约定（建议）

| 目标 | 约定 |
|------|------|
| `item_type` | 使用明确的业务前缀，例如 `manifest`, `svn_patch_merge` |
| `event_type` | 使用动词短语，例如 `manifest_uploaded`, `merge_completed` |
| `logbook.kv` 命名空间 | 使用工作流前缀，例如 `workflow.manifest`, `workflow.svn_merge` |

---

## 模板 1：manifest.csv 资产化

### 目标

- 将 `manifest.csv` 作为**可追踪资产**存储到 ArtifactStore
- 记录最新版本位置与哈希，便于后续工作流读取

### 推荐流程

1. **生成 manifest.csv（项目侧）**
2. **写入 ArtifactStore**
   - 使用 `artifacts_put` 写入（若为文本，直接 `content`）
3. **记录 Logbook**
   - `logbook_create_item`（若首次）
   - `logbook_attach` 记录附件引用
   - `logbook_add_event` 记录生成事件
4. **更新 KV 指针**
   - `logbook_set_kv` 记录最新版本指针

### 建议字段

**item_type**
- `manifest`

**event_type**
- `manifest_generated`
- `manifest_uploaded`

**KV 约定**
```
namespace: workflow.manifest
key: latest
value_json:
  {
    "artifact_uri": "scm/proj_a/manifest/manifest.csv",
    "sha256": "...",
    "size_bytes": 12345,
    "generated_at": "2026-02-05T12:00:00Z",
    "item_id": 123
  }
```

### 读取流程（示例）

1. `logbook_get_kv` 读取 `workflow.manifest/latest`
2. `artifacts_get` 获取内容（或 `evidence_read` 读取 evidence URI）

---

## 模板 2：svn-patch-merge 记账与查询

### 目标

- 通过 MCP 读写原语记录 merge 过程
- 使用 Logbook 统一保存 merge 结果与证据

### 推荐流程

1. **解析 patch_blob 元信息**
   - `scm_patch_blob_resolve`（输入 `sha256` 或 `evidence_uri`）
2. **按需物化 patch**
   - `scm_materialize_patch_blob`（若 diff 尚未物化）
3. **建立工作流条目**
   - `logbook_create_item`（item_type = `svn_patch_merge`）
4. **记录事件与附件**
   - `logbook_add_event`：记录 merge 动作
   - `logbook_attach`：记录 patch 证据或结果日志
5. **维护 KV 游标**
   - `logbook_set_kv`：记录最新处理 revision 或 merge 状态

### 建议字段

**item_type**
- `svn_patch_merge`

**event_type**
- `merge_requested`
- `patch_resolved`
- `patch_materialized`
- `merge_applied`
- `merge_conflict`
- `merge_completed`

**payload_json（示例）**
```json
{
  "source_type": "svn",
  "source_id": "1:r100",
  "sha256": "abc123...",
  "evidence_uri": "memory://patch_blobs/svn/1:r100/abc123...",
  "target_branch": "trunk",
  "merge_result": "success",
  "conflicts": [],
  "notes": "apply ok"
}
```

**KV 约定**
```
namespace: workflow.svn_merge
key: cursor:repo_1
value_json:
  {
    "last_rev": 100,
    "last_item_id": 456,
    "updated_at": "2026-02-05T12:00:00Z"
  }
```

---

## 组合调用建议（最小链路）

### manifest.csv 资产化

1. `artifacts_put` → 得到 `uri/sha256/size_bytes`
2. `logbook_create_item` → 获取 `item_id`
3. `logbook_attach` → 关联附件
4. `logbook_set_kv` → 更新 `workflow.manifest/latest`

### svn-patch-merge 记账

1. `scm_patch_blob_resolve`
2. `scm_materialize_patch_blob`（可选）
3. `logbook_create_item`
4. `logbook_add_event`（多次）
5. `logbook_attach`（补充证据）
6. `logbook_set_kv`（更新游标）

---

## 注意事项

- `logbook_set_kv` **要求显式提供 `value_json`**，避免误写空值
- 若使用 `evidence_upload` 上传文本证据，建议同时写入 `logbook_attach` 便于统一查询
- `scm_materialize_patch_blob` 可能返回 `NOT_IMPLEMENTED`，需在流程中允许降级或跳过

