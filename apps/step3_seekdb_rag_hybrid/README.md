# Step 3：证据检索加速层（seekdb / RAG / 与 OpenMemory 混合）

本 Step 的目标是把“可验证材料”（代码片段、patch、日志、规范、测试报告）做 **可检索索引**，
让 Agent 能做到：**先用 Step2 找策略/经验，再用 Step3 拉证据/上下文**，最后以 Step1 为准完成审计闭环。

适配你们现状：
- SVN 为主，GitLab 镜像用于分支预览与 MR 审查
- 不追求复杂基础设施，允许从小规模起步
- Step3 作为“加速层”可丢可重建，不影响 SoT

目录结构（本 zip）：
- docs/：部署与索引/检索规范、混合检索策略
- templates/：Python 同步/查询脚本模板
- contracts/：RAG 证据包（Evidence Packet）规范

更新时间：2026-01-26
