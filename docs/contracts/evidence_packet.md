# Evidence Packet（证据包）规范（SeekDB 分块输出）

目的：任何 Agent 的结论必须可被人审与复盘。

## 结构
1. Claim（结论）
2. Evidence（证据列表）
   - artifact_uri
   - sha256
   - source_id（rev/commit/event_id/attachment_id）
   - excerpt（最多 25 行或 2000 字，避免长贴）

### artifact_uri 允许的 Scheme

| Scheme | 说明 | 可解析性 |
|--------|------|----------|
| `memory://` | Logbook 内置存储（patch_blobs 或 attachments） | 本地可解析，直接查库 |
| `file://` | 本地文件路径 | 仅限同机/挂载卷 |
| `svn://` | SVN 仓库版本引用 | 需 SVN 客户端访问 |
| `git://` | Git 仓库 commit 引用 | 需 Git 客户端访问 |
| `https://` | HTTP(S) 远程资源 | 需网络可达 |

### memory:// URI 格式规范

`memory://` scheme 支持三种资源类型：

| 资源类型 | URI 格式 | 查询的表/来源 | 说明 |
|----------|----------|---------------|------|
| patch_blobs | `memory://patch_blobs/<source_type>/<source_id>/<sha256>` | `scm.patch_blobs` | SCM 补丁/diff 内容 |
| attachments | `memory://attachments/<attachment_id>/<sha256>` | `logbook.attachments` | 通用附件（截图、文档等） |
| docs | `memory://docs/<rel_path>/<sha256>` | 本地文件系统 | 规格/设计文档（如 contracts/, docs/） |

**示例**：
- `memory://patch_blobs/git/1:abc123/e3b0c44298fc...` - Git commit 的 diff
- `memory://attachments/12345/e3b0c44298fc...` - logbook 条目的附件
- `memory://docs/contracts/evidence_packet.md/a1b2c3d4e5f6...` - 规格文档

### memory://docs 技术决策（Canonical Scheme）

**选型决策**：采用 `memory://docs/...` 作为本地文档的 canonical scheme，而非 `git://`。

**理由**：
1. **统一性**：与现有 `memory://patch_blobs/...` 和 `memory://attachments/...` 保持一致的 URI 风格
2. **简洁性**：无需解析 git remote URL、branch 等复杂信息
3. **可扩展**：可支持非 git 管理的文档（如临时规格文档）
4. **兼容性**：回溯时可通过相对路径直接读取本地文件

**字段定义**：

| 字段 | 生成规则 | 示例 |
|------|----------|------|
| `source_type` | 固定为 `"docs"` | `"docs"` |
| `source_id` | 格式: `<docs_root>:<rel_path>` | `"contracts:evidence_packet.md"` |
| `artifact_uri` | 格式: `memory://docs/<rel_path>/<sha256>` | `memory://docs/contracts/evidence_packet.md/a1b2c3...` |
| `sha256` | 文件内容的 SHA256 哈希（64位十六进制） | `a1b2c3d4e5f67890...` |

**回溯步骤**：
1. **解析 URI**：从 `memory://docs/<rel_path>/<sha256>` 提取 `rel_path` 和 `sha256`
2. **定位文件**：根据配置的 docs_root（默认为仓库根目录）拼接 `rel_path`
3. **读取内容**：读取本地文件内容
4. **校验哈希**：计算内容 SHA256，与 URI 中的 `sha256` 比对
5. **返回或报错**：匹配则返回内容，不匹配则标记 `status: invalid`

**Git 集成（可选）**：
- 可通过 `git log -1 --format='%H' -- <rel_path>` 获取文件最后修改的 commit
- 存入 chunk metadata 的 `git_commit` 字段，便于精确版本追踪

**禁止的 Scheme**：`http://`（非加密）、`data://`（禁止内嵌大段内容）、`ftp://`

### 回溯步骤

当需要验证或复盘证据时，按以下步骤回溯原文：

1. **解析 artifact_uri**：提取 scheme 与路径
2. **校验 sha256**：若本地有缓存，比对 hash 确认未篡改
3. **按 scheme 获取内容**：
   - `memory://patch_blobs/...` → 查询 `scm.patch_blobs` 表，获取 `uri` 字段指向的制品
   - `memory://attachments/...` → 查询 `logbook.attachments` 表，获取 `uri` 字段指向的制品
   - `file://` → 读取本地文件系统
   - `svn://` → 执行 `svn cat <url>@<rev>` 获取指定版本内容
   - `git://` → 执行 `git show <commit>:<path>` 或调用 GitLab API
   - `https://` → HTTP GET 获取，若需认证则使用配置的 token
4. **比对摘要**：将获取的全文与 `excerpt` 对比，确认引用准确
5. **标记状态**：若 URI 不可达或 hash 不匹配，在 Evidence 中标记 `status: invalid`

### memory:// URI 回溯详细流程

```
memory://patch_blobs/{source_type}/{source_id}/{sha256}
    │
    ├─> SELECT uri FROM scm.patch_blobs WHERE sha256 = '{sha256}'
    │
    └─> 读取 uri 指向的制品（artifact key 或 file://）

memory://attachments/{attachment_id}/{sha256}
    │
    ├─> SELECT uri FROM logbook.attachments WHERE attachment_id = {attachment_id}
    │
    ├─> 校验 sha256 一致性
    │
    └─> 读取 uri 指向的制品（artifact key 或 file://）
```

3. Reasoning（推理过程摘要）
4. Risk & Next Steps（风险与下一步）
5. Verification（验证方法与通过标准）

说明：证据原文不入库，只引用指针；必要时在制品库/仓库中查看全文。

## SeekDB Chunk 输出与 Logbook Evidence URI 对应关系

SeekDB 分块模块（`seek_chunking.py`）的输出字段遵循 Logbook URI 规范（`src/engram/logbook/uri.py`），确保 chunk 结果可直接用于 evidence 引用。

### ChunkResult 字段映射

| SeekDB ChunkResult 字段 | Logbook Evidence 规范 | 说明 |
|------------------------|---------------------|------|
| `artifact_uri` | Canonical Evidence URI | 遵循 `memory://patch_blobs/...` 或 `memory://attachments/...` 格式 |
| `sha256` | 完整 SHA256 | 64 位十六进制，用于内容校验 |
| `source_id` | `<repo_id>:<rev/sha>` | 与 Logbook patch_blobs 的 source_id 格式一致 |
| `source_type` | `svn` / `git` / `logbook` | 来源类型标识 |

### artifact_uri Canonical 格式

SeekDB chunk 的 `artifact_uri` 字段使用 Logbook 定义的 Canonical Evidence URI 格式：

| 资源类型 | Canonical 格式 | Logbook 构建函数 |
|----------|----------------|----------------|
| patch_blobs | `memory://patch_blobs/<source_type>/<source_id>/<sha256>` | `build_evidence_uri()` |
| attachments | `memory://attachments/<attachment_id>/<sha256>` | `build_attachment_evidence_uri()` |

### 使用示例

```python
# 方式 1: SeekDB 自动构建（推荐）
chunks = chunk_diff(
    content=diff_content,
    source_type="git",
    source_id="1:abc123",
    sha256="e3b0c44298fc...",
    artifact_uri="",  # 留空，由 SeekDB 自动构建
)
# chunk.artifact_uri => "memory://patch_blobs/git/1:abc123/e3b0c44298fc..."

# 方式 2: 调用方传入 evidence_uri（优先级最高）
from engram_logbook.uri import build_evidence_uri

evidence_uri = build_evidence_uri("git", "1:abc123", "e3b0c44298fc...")
chunks = chunk_diff(
    content=diff_content,
    source_type="git",
    source_id="1:abc123",
    sha256="e3b0c44298fc...",
    artifact_uri="",
    evidence_uri=evidence_uri,  # 直接使用 Logbook 构建的 URI
)

# 方式 3: 附件类型
from seek_chunking import generate_attachment_artifact_uri

attachment_uri = generate_attachment_artifact_uri(
    attachment_id=12345,
    sha256="e3b0c44298fc...",
)
# => "memory://attachments/12345/e3b0c44298fc..."
```

### evidence_refs_json 集成

SeekDB chunk 输出可直接用于 governance/analysis 模块的 `evidence_refs_json`：

```python
# 从 chunk 构建 evidence reference
evidence_ref = {
    "artifact_uri": chunk.artifact_uri,  # 已是 canonical 格式
    "sha256": chunk.sha256,
    "source_id": chunk.source_id,
    "source_type": chunk.source_type,
    "kind": "patch",
    "excerpt": chunk.excerpt,
}
```

---

## SeekDB 依赖规范

本节定义 SeekDB 如何依赖 Evidence Packet 中的数据结构。

> **相关契约**：详细的 Logbook ↔ SeekDB 边界定义见 [logbook_seekdb_boundary.md](logbook_seekdb_boundary.md)

### SeekDB 读取的 Evidence 数据

| 数据源 | 必需字段 | SeekDB 用途 |
|--------|----------|-------------|
| patch_blobs | `sha256`, `evidence_uri`, `source_type`, `source_id` | Diff 分块与向量索引 |
| attachments | `attachment_id`, `sha256`, `evidence_uri` | 附件分块与向量索引 |

### ChunkResult 与 Evidence 字段映射

SeekDB 分块输出字段直接对应 Evidence Packet 规范：

| SeekDB ChunkResult | Evidence Packet 字段 | 约束 |
|--------------------|---------------------|------|
| `artifact_uri` | `artifact_uri` | 必须使用 `memory://` canonical 格式 |
| `sha256` | `sha256` | 64 位十六进制，与 Logbook 记录一致 |
| `source_id` | `source_id` | 格式：`<repo_id>:<rev/sha>` |
| `source_type` | `source_type` | `svn` / `git` / `logbook` |
| `excerpt` | `excerpt` | 最多 25 行或 2000 字 |

### 禁用开关行为（SEEKDB_ENABLE=0）

当 `SEEKDB_ENABLE=0` 时：

| 行为 | 说明 |
|------|------|
| Evidence URI 构建 | 正常工作（由 Logbook 提供） |
| SeekDB 索引 | 跳过，不构建索引 |
| SeekDB 测试 | 跳过，在报告中标记为 `SKIPPED` |
| Logbook 验收 | 正常通过，不依赖 SeekDB |

**验收命令示例**：

```bash
# Logbook-only 验收（无 SeekDB）
SEEKDB_ENABLE=0 make acceptance-logbook-only

# 统一栈验收（跳过 SeekDB）
SEEKDB_ENABLE=0 make acceptance-unified-min
```
