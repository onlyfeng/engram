[Kind] PROCEDURE | FACT | PITFALL | DECISION | REVIEW_GUIDE | REFLECTION
[Owner] u_xxx
[Module] client/ui/runtime/native/... (路径前缀或系统名)
[Visibility] team | private | org
[TTL] long | mid | short
[Confidence] high | mid | low

[Summary]
一句话结论（可检索，最大 200 字符）

[Details]
1) 要点（含边界条件，单条最大 500 字符）
2) 要点（含常见坑）
3) 验证口径（如何确认生效）

[Evidence]
- uri=memory://xxx 或 svn://xxx 或 git://xxx 或 https://xxx
  sha256=64位十六进制哈希（必填）
  event_id=...（可选）
  svn_rev=...（可选）
  git_commit=...（可选）
  mr=...（可选）

---
裁剪规则（由 memory_card.py 自动执行）:
- summary 最大 200 字符，超长截断
- 单条 detail 最大 500 字符，超长截断
- details 最多 10 条
- evidence 最多 10 条
- 总 Markdown 最大 4000 字符
- diff/log 内容自动替换为指针 + sha256（不存储原文）

payload_sha 计算:
- payload_sha = sha256(payload_md)
- 用于审计日志、outbox 队列、去重检测
