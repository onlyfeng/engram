# 概览：为什么需要 Step3

Step2（OpenMemory）适合存“经验/规则/口径”，但不适合存：
- 长 diff、长日志、完整规范、成百上千行代码片段

Step3 用 seekdb（或同类向量/混合检索引擎）存“可验证证据”，提供：
- 分块索引（chunking）+ metadata 过滤
- 混合检索（全文 + 向量）用于定位证据
- 输出 Evidence Packet，供 Agent 决策与人审

边界：
- Step3 索引可重建，失败不阻塞主流程
- 证据原文仍以文件/制品为准，Step3 只做索引与指针
