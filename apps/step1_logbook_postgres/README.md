# Step 1：团队级事实账本（Postgres / 单项目单库 / Schema 分层）

本 Step 的目标是用 **Postgres（服务器）** 取代 `manifest.csv / index.md` 这类“可被误改、难并发”的文件索引方式，
为所有 Agent/脚本提供 **可审计、可回放、强约束** 的事实来源（Single Source of Truth, SoT）。

- 适用：7 人并行开发、SVN 为主、GitLab 做分支预览与评审镜像
- 关键原则：
  1. **事实永远写入数据库（append / 状态机）**
  2. `manifest.csv / index.md` 仅作为 **可再生视图**（禁止手改）
  3. 大文件（patch/log/report）只存 **指针 + hash**，不存入库
  4. 与 Step2/Step3 的关系：Step1 提供“证据链与过程”，其他层只做“记忆/检索增强”，可丢可重建

目录结构（本 zip）：
- docs/：设计文档与规范
- sql/：Schema / DDL
- templates/：团队标准配置模板（user.config / role profile 等）
- scripts/：工具接口草案（可交由 Cursor Agent 实现）

> **注意**：本目录下的 `docker-compose.step1-test.yml` 仅用于 Step1 CI/本地测试。
> 旧版 `docker-compose.yml` 已作为 wrapper 保留，会自动引用新文件。
> 如需完整统一栈部署（Step1 + OpenMemory + Gateway），请使用根目录的 `make deploy`。

更新时间：2026-01-26
