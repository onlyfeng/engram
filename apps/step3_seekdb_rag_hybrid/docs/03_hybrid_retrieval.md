# 与 OpenMemory 的混合检索策略（推荐）

原则：**先策略后证据**
1) Step2（OpenMemory）检索：
   - 找到相关规则/坑点/审查口径/验证方式（TopK 5-10）
2) 由 Step2 结果生成“证据查询指令”：
   - 关键词、模块范围、source_id（rev/commit/event）
3) Step3（seekdb）检索：
   - 在限定 metadata 范围内召回证据 chunks（TopK 10-20）
4) 组装 Evidence Packet：
   - 结论必须引用证据（URI + sha256 + 行范围）
5) 需要落事实：回写 Step1（事件 + 附件）

降级策略：
- seekdb 不可用：直接从 Step1 附件指针中读取原文（按关键词 grep），但不再提供语义召回
