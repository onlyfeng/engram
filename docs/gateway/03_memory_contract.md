# 记忆写入/读取契约（Memory Gateway）

> **术语说明**：Memory Gateway 是 Gateway 组件的完整名称，后续简称 Gateway。详见 [命名规范](../architecture/naming.md)。

## 空间（Space）约定（团队可读默认）
- 团队空间：team:<project_key>（默认写入目标）
- 私有空间：private:<user_id>（开关关闭或策略不满足时的降级目标）
- 公共空间：org:shared（跨项目沉淀，后续启用）

## 记忆卡片模板（推荐 200–600 字）
字段（强制）：
- Kind：FACT / PROCEDURE / PITFALL / DECISION / REVIEW_GUIDE / REFLECTION
- Owner：<user_id>
- Module：系统/模块/路径前缀
- Summary：一句话可检索结论
- Details：3–5 条要点（含验证口径）
- Evidence：commit/rev/mr/event_id/patch_sha/uri/hash
  - **uri 可解析性要求**：必须为可回溯的有效 URI（scheme 限 `memory://`、`svn://`、`git://`、`https://`）
  - **hash 校验**：sha256 必填，用于验证原文完整性
  - **降级策略**：team/private 空间不存储原文内容，仅存储指针（uri）+ hash；需查看原文时回跳 Logbook 或源仓库
  - **URI 格式规范**：
    - **patch_blobs / attachment URI 格式详见**：[Evidence Packet 规范](../contracts/evidence_packet.md#memory-uri-格式规范)
    - **URI 语法与解析规则详见**：[`engram_logbook.uri`](../../apps/logbook_postgres/scripts/engram_logbook/uri.py) 模块（Logbook 为 URI grammar 的唯一规范 owner）
- Confidence：high/mid/low
- Visibility：team/private/org
- TTL：long/mid/short（bulk 变更默认 short）

### Evidence URI 与 Logbook 的关系

Evidence URI 格式由 Logbook 层统一定义，Gateway 仅作为调用方使用。

**核心约束**：
1. **可回溯性**：Gateway 存储的 evidence URI 必须能被 Logbook 正确解析并定位到原始记录
2. **格式一致性**：所有 URI 构建与解析必须使用 `engram_logbook.uri` 模块的函数
3. **审计追溯**：统一的 URI 格式确保跨系统的 evidence 链路完整可追溯

**规范文档索引**：
- **Evidence Packet 结构**：[docs/contracts/evidence_packet.md](../contracts/evidence_packet.md)
- **URI 语法规范**：[`engram_logbook.uri`](../../apps/logbook_postgres/scripts/engram_logbook/uri.py)（含 `build_evidence_uri()`、`parse_attachment_evidence_uri()` 等）
- **Gateway ↔ Logbook 边界契约**：[docs/contracts/gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md#uri-grammar-归属声明)

## 写入规则（默认）
- 默认写 team:<project>
- team_write_enabled=false 时：自动重定向写 private:<actor>
- 任何写入必须带 Evidence（至少一个）
- 禁止写入：整段 diff、完整日志（只写指针 + hash）

## 读取规则（默认）
- 先查 team:<project>（Top 5）
- 再查 private:<actor>（Top 5）
- 输出必须包含 Evidence 指针，便于回跳 Logbook

---

## Memory Card 生成器 (memory_card.py)

### 核心功能
`../gateway/gateway/memory_card.py` 提供记忆卡片的标准化生成与裁剪：

1. **Markdown 生成**：将结构化输入转换为符合模板规范的 Markdown
2. **内容裁剪**：自动裁剪超长内容，保障存储效率
3. **SHA 计算**：`payload_sha = sha256(payload_md)` 用于审计与去重

### 裁剪策略

| 字段 | 限制 | 处理方式 |
|------|------|----------|
| Summary | 最大 200 字符 | 超长截断，添加 `[内容已截断]` |
| 单条 Detail | 最大 500 字符 | 超长截断 |
| Details 条目数 | 最多 10 条 | 超出部分丢弃 |
| Evidence 条目数 | 最多 10 条 | 超出部分丢弃 |
| 总 Markdown | 最大 4000 字符 | 强制截断 |
| diff 内容 | 禁止存储原文 | 替换为指针 + sha256 |
| log 内容 | 禁止存储原文 | 替换为指针 + sha256 |

### 使用示例

```python
from gateway.memory_card import generate_memory_markdown, create_memory_card

# 方式一：直接生成 Markdown 和 SHA
payload_md, payload_sha = generate_memory_markdown(
    kind="PROCEDURE",
    owner="u_12345",
    module="client/ui/button",
    summary="Button 组件需要显式设置 aria-label 以支持无障碍访问",
    details=[
        "所有交互按钮必须设置 aria-label 属性",
        "图标按钮尤其重要，因为没有可见文字",
        "验证：使用屏幕阅读器测试按钮是否可朗读"
    ],
    evidence=[{
        "uri": "git://repo/commit/abc123",
        "sha256": "a1b2c3d4..." * 8,  # 64 字符
        "git_commit": "abc123",
        "mr": "MR-456"
    }],
    confidence="high",
    visibility="team",
    ttl="long"
)

# 方式二：创建 MemoryCard 对象（更灵活）
card = create_memory_card(
    kind="PITFALL",
    owner="u_12345",
    module="runtime/memory",
    summary="内存泄漏场景：未释放的事件监听器",
    details=["..."],
    evidence=[{"uri": "...", "sha256": "..."}]
)

# 验证卡片
errors = card.validate()
if errors:
    print(f"验证失败: {errors}")

# 生成 Markdown
markdown = card.to_markdown()

# 计算 SHA（用于审计和 outbox）
sha = card.compute_payload_sha()

# 获取裁剪日志
trim_logs = card.get_trim_logs()
```

### 与 Outbox 集成

`payload_sha` 用于：
- `governance.write_audit.payload_sha`：审计日志中的内容哈希
- `logbook.outbox_memory.payload_sha`：Outbox 队列中的内容哈希
- 去重检测：相同 SHA 的记忆不重复写入
