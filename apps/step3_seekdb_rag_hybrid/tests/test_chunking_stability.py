"""
test_chunking_stability.py - Step3 分块稳定性测试

测试内容:
1. Diff/Log/Markdown 分块稳定性
   - 相同输入产生相同 chunk 数量
   - chunk_idx 顺序正确（0, 1, 2, ...）
   - chunk_id 稳定（同一输入多次调用结果一致）
2. Excerpt 长度约束
   - excerpt 行数 <= 25
   - excerpt 字符数 <= 2000
3. Memory://attachments resolver 往返测试
   - 生成 -> 解析 -> 验证一致性
"""

import hashlib
import sys
from pathlib import Path

import pytest

# 添加父目录到路径
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

from step3_chunking import (
    CHUNKING_VERSION,
    CHUNK_ID_NAMESPACE,
    ChunkResult,
    chunk_content,
    chunk_diff,
    chunk_log,
    chunk_markdown,
    compute_sha256,
    generate_artifact_uri,
    generate_chunk_id,
    generate_excerpt,
    parse_chunk_id,
)


# ============================================================
# 固定测试样例
# ============================================================

# Git Diff 样例
SAMPLE_DIFF_GIT = """\
diff --git a/src/main.py b/src/main.py
index abc1234..def5678 100644
--- a/src/main.py
+++ b/src/main.py
@@ -10,6 +10,8 @@ import os
 import sys
 
+# 新增功能模块导入
+from utils import helper
 
 def main():
     print("Hello World")
@@ -25,3 +27,10 @@ def main():
     return 0
+
+def new_function():
+    '''新增的辅助函数'''
+    result = helper.process()
+    return result
diff --git a/src/utils.py b/src/utils.py
new file mode 100644
index 0000000..abcdef1
--- /dev/null
+++ b/src/utils.py
@@ -0,0 +1,15 @@
+'''工具模块'''
+
+class Helper:
+    def process(self):
+        return "processed"
+
+helper = Helper()
"""

# SVN Diff 样例
SAMPLE_DIFF_SVN = """\
Index: trunk/src/config.py
===================================================================
--- trunk/src/config.py	(revision 100)
+++ trunk/src/config.py	(revision 101)
@@ -5,6 +5,7 @@
 DATABASE_HOST = "localhost"
 DATABASE_PORT = 5432
+DATABASE_NAME = "engram"
 
 def get_config():
     return {
Index: trunk/src/db.py
===================================================================
--- trunk/src/db.py	(revision 100)
+++ trunk/src/db.py	(revision 101)
@@ -1,5 +1,8 @@
 import psycopg2
 
+from config import DATABASE_NAME
+
+
 def connect():
-    return psycopg2.connect(host="localhost")
+    return psycopg2.connect(host="localhost", dbname=DATABASE_NAME)
"""

# Log 样例（含错误）
SAMPLE_LOG_ERROR = """\
2026-01-28 10:00:00 INFO  Starting application...
2026-01-28 10:00:01 INFO  Loading configuration...
2026-01-28 10:00:02 INFO  Connecting to database...
2026-01-28 10:00:03 ERROR Database connection failed: Connection refused
2026-01-28 10:00:03 ERROR Traceback (most recent call last):
2026-01-28 10:00:03 ERROR   File "db.py", line 15, in connect
2026-01-28 10:00:03 ERROR     conn = psycopg2.connect(...)
2026-01-28 10:00:03 ERROR psycopg2.OperationalError: could not connect to server
2026-01-28 10:00:04 INFO  Retrying connection...
2026-01-28 10:00:05 WARN  Connection attempt 2 of 3
2026-01-28 10:00:06 ERROR Connection failed again
2026-01-28 10:00:07 FATAL Maximum retries exceeded, shutting down
"""

# Log 样例（无错误）
SAMPLE_LOG_NORMAL = """\
2026-01-28 10:00:00 INFO  Starting application...
2026-01-28 10:00:01 INFO  Loading configuration...
2026-01-28 10:00:02 INFO  Connecting to database...
2026-01-28 10:00:03 INFO  Database connected successfully
2026-01-28 10:00:04 INFO  Initializing modules...
2026-01-28 10:00:05 INFO  Application started
2026-01-28 10:00:10 INFO  Processing request #1
2026-01-28 10:00:11 INFO  Request #1 completed in 50ms
2026-01-28 10:00:15 INFO  Processing request #2
2026-01-28 10:00:16 INFO  Request #2 completed in 45ms
"""

# Markdown 样例
SAMPLE_MARKDOWN = """\
# Step3 设计文档

这是 Step3 的设计文档，描述 RAG 混合检索的实现。

## 1. 概述

本模块实现了基于 SeekDB 的混合检索功能。

### 1.1 核心功能

- 向量检索（语义相似度）
- 关键词检索（BM25）
- 混合排序

### 1.2 依赖项

依赖以下组件：
- SeekDB 向量数据库
- PostgreSQL 元数据存储
- Step1 Logbook 模块

## 2. 架构设计

系统采用分层架构。

### 2.1 索引层

负责数据摄入和索引构建。

### 2.2 检索层

负责查询处理和结果排序。

## 3. API 设计

提供 RESTful API 接口。

### 3.1 索引 API

`POST /api/v1/index` - 索引新文档

### 3.2 查询 API

`POST /api/v1/search` - 执行检索

## 4. 总结

本文档描述了 Step3 的整体设计。
"""


# ============================================================
# 测试: Diff 分块稳定性
# ============================================================

class TestDiffChunkingStability:
    """Diff 分块稳定性测试"""

    @pytest.fixture
    def git_diff_params(self):
        """Git diff 分块参数"""
        content = SAMPLE_DIFF_GIT
        sha256 = compute_sha256(content)
        return {
            "content": content,
            "source_type": "git",
            "source_id": "repo1:abc123",
            "sha256": sha256,
            "artifact_uri": "artifact://scm/proj_a/1/git/abc123/diff.patch",
        }

    @pytest.fixture
    def svn_diff_params(self):
        """SVN diff 分块参数"""
        content = SAMPLE_DIFF_SVN
        sha256 = compute_sha256(content)
        return {
            "content": content,
            "source_type": "svn",
            "source_id": "repo2:r101",
            "sha256": sha256,
            "artifact_uri": "artifact://scm/proj_b/2/svn/r101/diff.patch",
        }

    def test_git_diff_chunk_count_stable(self, git_diff_params):
        """测试 Git diff 分块数量稳定"""
        chunks1 = chunk_diff(**git_diff_params)
        chunks2 = chunk_diff(**git_diff_params)

        assert len(chunks1) > 0, "应该产生至少一个 chunk"
        assert len(chunks1) == len(chunks2), "相同输入应产生相同数量的 chunk"

    def test_git_diff_chunk_idx_order(self, git_diff_params):
        """测试 Git diff chunk_idx 顺序正确"""
        chunks = chunk_diff(**git_diff_params)

        # chunk_idx 应该从 0 开始连续递增
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_idx == i, f"chunk_idx 应该是 {i}，实际是 {chunk.chunk_idx}"

    def test_git_diff_chunk_id_stable(self, git_diff_params):
        """测试 Git diff chunk_id 稳定性"""
        chunks1 = chunk_diff(**git_diff_params)
        chunks2 = chunk_diff(**git_diff_params)

        for c1, c2 in zip(chunks1, chunks2):
            assert c1.chunk_id == c2.chunk_id, f"chunk_id 应该稳定: {c1.chunk_id} vs {c2.chunk_id}"

    def test_svn_diff_chunk_count_stable(self, svn_diff_params):
        """测试 SVN diff 分块数量稳定"""
        chunks1 = chunk_diff(**svn_diff_params)
        chunks2 = chunk_diff(**svn_diff_params)

        assert len(chunks1) > 0, "应该产生至少一个 chunk"
        assert len(chunks1) == len(chunks2), "相同输入应产生相同数量的 chunk"

    def test_svn_diff_chunk_idx_order(self, svn_diff_params):
        """测试 SVN diff chunk_idx 顺序正确"""
        chunks = chunk_diff(**svn_diff_params)

        for i, chunk in enumerate(chunks):
            assert chunk.chunk_idx == i, f"chunk_idx 应该是 {i}，实际是 {chunk.chunk_idx}"

    def test_svn_diff_chunk_id_stable(self, svn_diff_params):
        """测试 SVN diff chunk_id 稳定性"""
        chunks1 = chunk_diff(**svn_diff_params)
        chunks2 = chunk_diff(**svn_diff_params)

        for c1, c2 in zip(chunks1, chunks2):
            assert c1.chunk_id == c2.chunk_id, f"chunk_id 应该稳定"

    def test_diff_per_file_chunking(self, git_diff_params):
        """测试 diff 按文件+hunk 分块"""
        chunks = chunk_diff(**git_diff_params)

        # Git diff 样例有 2 个文件，应该产生 >= 2 个 chunk
        # main.py 有 2 个 hunk，utils.py 有 1 个 hunk
        assert len(chunks) >= 2, f"应该按文件分块，产生 >= 2 个 chunk，实际 {len(chunks)}"


# ============================================================
# 测试: Log 分块稳定性
# ============================================================

class TestLogChunkingStability:
    """Log 分块稳定性测试"""

    @pytest.fixture
    def log_error_params(self):
        """含错误 log 分块参数"""
        content = SAMPLE_LOG_ERROR
        sha256 = compute_sha256(content)
        return {
            "content": content,
            "source_id": "build:job123",
            "sha256": sha256,
            "artifact_uri": "artifact://logs/build/job123.log",
        }

    @pytest.fixture
    def log_normal_params(self):
        """无错误 log 分块参数"""
        content = SAMPLE_LOG_NORMAL
        sha256 = compute_sha256(content)
        return {
            "content": content,
            "source_id": "build:job456",
            "sha256": sha256,
            "artifact_uri": "artifact://logs/build/job456.log",
        }

    def test_log_error_chunk_count_stable(self, log_error_params):
        """测试含错误 log 分块数量稳定"""
        chunks1 = chunk_log(**log_error_params)
        chunks2 = chunk_log(**log_error_params)

        assert len(chunks1) > 0, "应该产生至少一个 chunk"
        assert len(chunks1) == len(chunks2), "相同输入应产生相同数量的 chunk"

    def test_log_error_chunk_idx_order(self, log_error_params):
        """测试含错误 log chunk_idx 顺序正确"""
        chunks = chunk_log(**log_error_params)

        for i, chunk in enumerate(chunks):
            assert chunk.chunk_idx == i, f"chunk_idx 应该是 {i}"

    def test_log_error_chunk_id_stable(self, log_error_params):
        """测试含错误 log chunk_id 稳定性"""
        chunks1 = chunk_log(**log_error_params)
        chunks2 = chunk_log(**log_error_params)

        for c1, c2 in zip(chunks1, chunks2):
            assert c1.chunk_id == c2.chunk_id, "chunk_id 应该稳定"

    def test_log_normal_chunk_count_stable(self, log_normal_params):
        """测试无错误 log 分块数量稳定"""
        chunks1 = chunk_log(**log_normal_params)
        chunks2 = chunk_log(**log_normal_params)

        assert len(chunks1) > 0, "应该产生至少一个 chunk"
        assert len(chunks1) == len(chunks2), "相同输入应产生相同数量的 chunk"

    def test_log_normal_chunk_idx_order(self, log_normal_params):
        """测试无错误 log chunk_idx 顺序正确"""
        chunks = chunk_log(**log_normal_params)

        for i, chunk in enumerate(chunks):
            assert chunk.chunk_idx == i, f"chunk_idx 应该是 {i}"

    def test_log_error_extracts_error_blocks(self, log_error_params):
        """测试含错误 log 提取错误段落"""
        chunks = chunk_log(**log_error_params)

        # 至少一个 chunk 包含 ERROR 或 FATAL
        error_chunks = [
            c for c in chunks
            if "ERROR" in c.content or "FATAL" in c.content or "error" in c.content.lower()
        ]
        assert len(error_chunks) > 0, "应该提取包含错误的 chunk"


# ============================================================
# 测试: Markdown 分块稳定性
# ============================================================

class TestMarkdownChunkingStability:
    """Markdown 分块稳定性测试"""

    @pytest.fixture
    def md_params(self):
        """Markdown 分块参数"""
        content = SAMPLE_MARKDOWN
        sha256 = compute_sha256(content)
        return {
            "content": content,
            "source_id": "doc:step3-design",
            "sha256": sha256,
            "artifact_uri": "artifact://docs/step3/design.md",
        }

    def test_md_chunk_count_stable(self, md_params):
        """测试 Markdown 分块数量稳定"""
        chunks1 = chunk_markdown(**md_params)
        chunks2 = chunk_markdown(**md_params)

        assert len(chunks1) > 0, "应该产生至少一个 chunk"
        assert len(chunks1) == len(chunks2), "相同输入应产生相同数量的 chunk"

    def test_md_chunk_idx_order(self, md_params):
        """测试 Markdown chunk_idx 顺序正确"""
        chunks = chunk_markdown(**md_params)

        for i, chunk in enumerate(chunks):
            assert chunk.chunk_idx == i, f"chunk_idx 应该是 {i}"

    def test_md_chunk_id_stable(self, md_params):
        """测试 Markdown chunk_id 稳定性"""
        chunks1 = chunk_markdown(**md_params)
        chunks2 = chunk_markdown(**md_params)

        for c1, c2 in zip(chunks1, chunks2):
            assert c1.chunk_id == c2.chunk_id, "chunk_id 应该稳定"

    def test_md_splits_by_heading(self, md_params):
        """测试 Markdown 按标题分块"""
        chunks = chunk_markdown(**md_params)

        # Markdown 样例有多个 h2 标题，应该按 h2 分块
        # 预期分块: 概述, 架构设计, API 设计, 总结 等
        assert len(chunks) >= 3, f"应该按标题分块，产生 >= 3 个 chunk，实际 {len(chunks)}"


# ============================================================
# 测试: 通用入口 chunk_content
# ============================================================

class TestChunkContentUnified:
    """通用分块入口测试"""

    def test_chunk_content_diff_type(self):
        """测试 chunk_content 处理 diff 类型"""
        sha256 = compute_sha256(SAMPLE_DIFF_GIT)
        chunks = chunk_content(
            content=SAMPLE_DIFF_GIT,
            content_type="diff",
            source_type="git",
            source_id="repo1:abc",
            sha256=sha256,
            artifact_uri="artifact://test.diff",
        )
        assert len(chunks) > 0
        assert all(c.source_type == "git" for c in chunks)

    def test_chunk_content_log_type(self):
        """测试 chunk_content 处理 log 类型"""
        sha256 = compute_sha256(SAMPLE_LOG_ERROR)
        chunks = chunk_content(
            content=SAMPLE_LOG_ERROR,
            content_type="log",
            source_type="logbook",
            source_id="build:123",
            sha256=sha256,
            artifact_uri="artifact://test.log",
        )
        assert len(chunks) > 0
        assert all(c.source_type == "logbook" for c in chunks)

    def test_chunk_content_md_type(self):
        """测试 chunk_content 处理 md 类型"""
        sha256 = compute_sha256(SAMPLE_MARKDOWN)
        chunks = chunk_content(
            content=SAMPLE_MARKDOWN,
            content_type="md",
            source_type="logbook",
            source_id="doc:test",
            sha256=sha256,
            artifact_uri="artifact://test.md",
        )
        assert len(chunks) > 0
        assert all(c.source_type == "logbook" for c in chunks)

    def test_chunk_content_text_type(self):
        """测试 chunk_content 处理 text 类型"""
        content = "Hello World\n" * 100
        sha256 = compute_sha256(content)
        chunks = chunk_content(
            content=content,
            content_type="text",
            source_type="other",
            source_id="test:plain",
            sha256=sha256,
            artifact_uri="artifact://test.txt",
        )
        assert len(chunks) > 0


# ============================================================
# 测试: Excerpt 长度约束
# ============================================================

class TestExcerptConstraints:
    """Excerpt 长度约束测试"""

    # 约束常量
    MAX_EXCERPT_LINES = 25
    MAX_EXCERPT_CHARS = 2000

    def _count_lines(self, text: str) -> int:
        """统计行数"""
        if not text:
            return 0
        return len(text.split("\n"))

    def test_excerpt_line_limit_diff(self):
        """测试 diff excerpt 行数约束"""
        sha256 = compute_sha256(SAMPLE_DIFF_GIT)
        chunks = chunk_diff(
            content=SAMPLE_DIFF_GIT,
            source_type="git",
            source_id="test:1",
            sha256=sha256,
            artifact_uri="test://diff",
        )

        for chunk in chunks:
            lines = self._count_lines(chunk.excerpt)
            assert lines <= self.MAX_EXCERPT_LINES, (
                f"excerpt 行数 ({lines}) 超过限制 ({self.MAX_EXCERPT_LINES})"
            )

    def test_excerpt_char_limit_diff(self):
        """测试 diff excerpt 字符数约束"""
        sha256 = compute_sha256(SAMPLE_DIFF_GIT)
        chunks = chunk_diff(
            content=SAMPLE_DIFF_GIT,
            source_type="git",
            source_id="test:1",
            sha256=sha256,
            artifact_uri="test://diff",
        )

        for chunk in chunks:
            assert len(chunk.excerpt) <= self.MAX_EXCERPT_CHARS, (
                f"excerpt 字符数 ({len(chunk.excerpt)}) 超过限制 ({self.MAX_EXCERPT_CHARS})"
            )

    def test_excerpt_line_limit_log(self):
        """测试 log excerpt 行数约束"""
        sha256 = compute_sha256(SAMPLE_LOG_ERROR)
        chunks = chunk_log(
            content=SAMPLE_LOG_ERROR,
            source_id="test:1",
            sha256=sha256,
            artifact_uri="test://log",
        )

        for chunk in chunks:
            lines = self._count_lines(chunk.excerpt)
            assert lines <= self.MAX_EXCERPT_LINES, (
                f"excerpt 行数 ({lines}) 超过限制 ({self.MAX_EXCERPT_LINES})"
            )

    def test_excerpt_char_limit_log(self):
        """测试 log excerpt 字符数约束"""
        sha256 = compute_sha256(SAMPLE_LOG_ERROR)
        chunks = chunk_log(
            content=SAMPLE_LOG_ERROR,
            source_id="test:1",
            sha256=sha256,
            artifact_uri="test://log",
        )

        for chunk in chunks:
            assert len(chunk.excerpt) <= self.MAX_EXCERPT_CHARS, (
                f"excerpt 字符数 ({len(chunk.excerpt)}) 超过限制 ({self.MAX_EXCERPT_CHARS})"
            )

    def test_excerpt_line_limit_md(self):
        """测试 markdown excerpt 行数约束"""
        sha256 = compute_sha256(SAMPLE_MARKDOWN)
        chunks = chunk_markdown(
            content=SAMPLE_MARKDOWN,
            source_id="test:1",
            sha256=sha256,
            artifact_uri="test://md",
        )

        for chunk in chunks:
            lines = self._count_lines(chunk.excerpt)
            assert lines <= self.MAX_EXCERPT_LINES, (
                f"excerpt 行数 ({lines}) 超过限制 ({self.MAX_EXCERPT_LINES})"
            )

    def test_excerpt_char_limit_md(self):
        """测试 markdown excerpt 字符数约束"""
        sha256 = compute_sha256(SAMPLE_MARKDOWN)
        chunks = chunk_markdown(
            content=SAMPLE_MARKDOWN,
            source_id="test:1",
            sha256=sha256,
            artifact_uri="test://md",
        )

        for chunk in chunks:
            assert len(chunk.excerpt) <= self.MAX_EXCERPT_CHARS, (
                f"excerpt 字符数 ({len(chunk.excerpt)}) 超过限制 ({self.MAX_EXCERPT_CHARS})"
            )

    def test_generate_excerpt_truncation(self):
        """测试 generate_excerpt 截断功能"""
        # 生成超长内容
        long_content = "A" * 3000

        excerpt = generate_excerpt(long_content, "text")
        assert len(excerpt) <= 200, "excerpt 应该被截断到 200 字符以内"

        # 使用 diff 类型测试 "..." 后缀（diff 会先生成 key_lines，可能超过限制）
        long_diff = "\n".join([
            "diff --git a/test.py b/test.py",
            "--- a/test.py",
            "+++ b/test.py",
            "@@ -1,100 +1,100 @@",
        ] + [f"+line {i} with some content to make it longer" for i in range(100)])

        diff_excerpt = generate_excerpt(long_diff, "diff")
        assert len(diff_excerpt) <= 200, "diff excerpt 应该被截断到 200 字符以内"

    def test_generate_excerpt_empty(self):
        """测试 generate_excerpt 处理空内容"""
        excerpt = generate_excerpt("", "text")
        assert excerpt == ""

        excerpt = generate_excerpt("   ", "text")
        assert len(excerpt) <= 200


# ============================================================
# 测试: chunk_id 生成和解析
# ============================================================

class TestChunkIdRoundTrip:
    """chunk_id 生成和解析往返测试"""

    def test_generate_parse_roundtrip(self):
        """测试 chunk_id 生成和解析往返"""
        # 生成 chunk_id
        chunk_id = generate_chunk_id(
            source_type="svn",
            source_id="1:12345",
            sha256="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            chunk_idx=5,
        )

        # 解析 chunk_id
        parsed = parse_chunk_id(chunk_id)

        assert parsed is not None, "应该能解析 chunk_id"
        assert parsed["namespace"] == CHUNK_ID_NAMESPACE
        assert parsed["source_type"] == "svn"
        assert parsed["source_id"] == "1:12345"  # 还原后的 source_id
        assert parsed["sha256_prefix"] == "abcdef123456"  # 前 12 位
        assert parsed["chunking_version"] == CHUNKING_VERSION
        assert parsed["chunk_idx"] == 5

    def test_chunk_id_deterministic(self):
        """测试 chunk_id 生成是确定性的"""
        params = {
            "source_type": "git",
            "source_id": "repo:commit",
            "sha256": "1234567890abcdef" * 4,
            "chunk_idx": 0,
        }

        id1 = generate_chunk_id(**params)
        id2 = generate_chunk_id(**params)

        assert id1 == id2, "相同参数应生成相同 chunk_id"

    def test_chunk_id_unique_by_idx(self):
        """测试不同 chunk_idx 生成不同 chunk_id"""
        base_params = {
            "source_type": "git",
            "source_id": "repo:commit",
            "sha256": "1234567890abcdef" * 4,
        }

        id0 = generate_chunk_id(**base_params, chunk_idx=0)
        id1 = generate_chunk_id(**base_params, chunk_idx=1)

        assert id0 != id1, "不同 chunk_idx 应生成不同 chunk_id"

    def test_parse_invalid_chunk_id(self):
        """测试解析无效 chunk_id"""
        assert parse_chunk_id("invalid") is None
        assert parse_chunk_id("a:b:c") is None
        assert parse_chunk_id("") is None
        assert parse_chunk_id("a:b:c:d:e:f:g") is None  # 太多组件


# ============================================================
# 测试: artifact_uri 生成
# ============================================================

class TestArtifactUriGeneration:
    """artifact_uri 生成测试（遵循 Step1 Canonical Evidence URI 规范）"""

    def test_generate_canonical_patch_blobs_uri(self):
        """测试生成 canonical patch_blobs URI"""
        uri = generate_artifact_uri(
            original_uri="file:///data/patch.diff",
            source_type="svn",
            source_id="1:100",
            sha256="abcdef1234567890" * 4,
        )

        # 验证 canonical 格式: memory://patch_blobs/<source_type>/<source_id>/<sha256>
        assert uri.startswith("memory://patch_blobs/"), f"应该以 memory://patch_blobs/ 开头，实际: {uri}"
        assert "/svn/" in uri, "应该包含 source_type"
        assert "/1:100/" in uri, "source_id 应该保持原格式（不替换冒号）"
        assert uri.endswith("abcdef1234567890" * 4), "应该包含完整 sha256"

    def test_preserve_canonical_memory_uri(self):
        """测试保留已有的 canonical memory:// URI"""
        original = "memory://patch_blobs/git/2:abc123/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        uri = generate_artifact_uri(
            original_uri=original,
            source_type="svn",
            source_id="1:100",
            sha256="abcdef1234567890" * 4,
        )

        assert uri == original, "已有的 canonical memory://patch_blobs/ URI 应该被保留"

    def test_preserve_attachment_memory_uri(self):
        """测试保留已有的 attachment memory:// URI"""
        original = "memory://attachments/12345/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        uri = generate_artifact_uri(
            original_uri=original,
            source_type="logbook",
            source_id="attach:12345",
            sha256="abcdef1234567890" * 4,
        )

        assert uri == original, "已有的 memory://attachments/ URI 应该被保留"

    def test_evidence_uri_parameter_priority(self):
        """测试 evidence_uri 参数优先级"""
        evidence_uri = "memory://patch_blobs/git/1:abc/sha256hash"
        uri = generate_artifact_uri(
            original_uri="file:///data/patch.diff",
            source_type="svn",
            source_id="2:200",
            sha256="different_sha256" * 4,
            evidence_uri=evidence_uri,
        )

        assert uri == evidence_uri, "传入的 evidence_uri 应该被优先使用"

    def test_attachment_source_id_generates_attachments_uri(self):
        """测试 source_type=logbook 且 source_id=attachment:<int> 时生成 attachments URI"""
        sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        uri = generate_artifact_uri(
            original_uri="file:///data/attachment.txt",
            source_type="logbook",
            source_id="attachment:12345",
            sha256=sha256,
        )

        expected = f"memory://attachments/12345/{sha256}"
        assert uri == expected, f"应该生成 attachments URI，实际: {uri}"

    def test_attachment_source_id_various_ids(self):
        """测试不同 attachment_id 的 attachments URI 生成"""
        sha256 = "abcdef1234567890" * 4

        test_cases = [
            ("attachment:1", "1"),
            ("attachment:999999", "999999"),
            ("attachment:0", "0"),
        ]

        for source_id, expected_id in test_cases:
            uri = generate_artifact_uri(
                original_uri="",
                source_type="logbook",
                source_id=source_id,
                sha256=sha256,
            )
            expected = f"memory://attachments/{expected_id}/{sha256}"
            assert uri == expected, f"source_id={source_id} 应该生成正确的 attachments URI"

    def test_attachment_source_id_evidence_uri_priority(self):
        """测试 attachment 格式下 evidence_uri 仍为最高优先级"""
        evidence_uri = "memory://custom/path/to/evidence"
        uri = generate_artifact_uri(
            original_uri="",
            source_type="logbook",
            source_id="attachment:12345",
            sha256="abc123def456",
            evidence_uri=evidence_uri,
        )

        assert uri == evidence_uri, "evidence_uri 应该优先于 attachment 格式检测"

    def test_attachment_source_id_non_logbook_type(self):
        """测试非 logbook 类型不会生成 attachments URI"""
        sha256 = "abcdef1234567890" * 4
        uri = generate_artifact_uri(
            original_uri="",
            source_type="svn",  # 非 logbook
            source_id="attachment:12345",
            sha256=sha256,
        )

        # 应该生成 patch_blobs URI，而不是 attachments
        assert uri.startswith("memory://patch_blobs/"), f"非 logbook 类型应该生成 patch_blobs URI，实际: {uri}"

    def test_attachment_source_id_invalid_format(self):
        """测试无效 attachment 格式仍生成 patch_blobs URI"""
        sha256 = "abcdef1234567890" * 4

        invalid_formats = [
            "attachment:",       # 缺少 ID
            "attachment:abc",    # 非数字 ID
            "attachments:123",   # 拼写错误
            "ATTACHMENT:123",    # 大写
            "attach:123",        # 前缀错误
            "123",               # 纯数字
        ]

        for source_id in invalid_formats:
            uri = generate_artifact_uri(
                original_uri="",
                source_type="logbook",
                source_id=source_id,
                sha256=sha256,
            )
            assert uri.startswith("memory://patch_blobs/"), (
                f"无效格式 source_id={source_id} 应该生成 patch_blobs URI，实际: {uri}"
            )

    def test_attachment_source_id_memory_uri_preserved(self):
        """测试已有 memory:// URI 时保持不变（优先级 2 高于 attachment 检测）"""
        original = "memory://attachments/99999/existing_sha256"
        uri = generate_artifact_uri(
            original_uri=original,
            source_type="logbook",
            source_id="attachment:12345",  # 不同的 attachment_id
            sha256="different_sha256",
        )

        assert uri == original, "已有的 memory:// URI 应该被保留"


# ============================================================
# 测试: memory://attachments resolver 往返测试
# ============================================================

class TestMemoryAttachmentsResolver:
    """memory://attachments resolver 往返测试（遵循 Step1 Canonical Evidence URI 规范）

    Canonical Attachment Evidence URI 格式:
        memory://attachments/<attachment_id>/<sha256>

    注意: 这些测试不需要实际数据库连接，测试的是 URI 解析逻辑
    """

    def test_attachment_uri_format(self):
        """测试 canonical attachment URI 格式"""
        # 生成 canonical attachment URI
        attachment_id = 12345
        sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

        # 使用 Step3 的生成函数（应遵循 Step1 规范）
        from step3_chunking import generate_attachment_artifact_uri
        uri = generate_attachment_artifact_uri(attachment_id, sha256)

        # 验证 canonical 格式
        assert uri == f"memory://attachments/{attachment_id}/{sha256}"
        assert uri.startswith("memory://attachments/")
        parts = uri.replace("memory://attachments/", "").split("/")
        assert len(parts) == 2
        assert parts[0] == str(attachment_id)
        assert parts[1] == sha256

    def test_attachment_uri_roundtrip_format(self):
        """测试 attachment URI 往返格式一致性"""
        from step3_chunking import generate_attachment_artifact_uri

        test_cases = [
            (12345, "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"),
            (1, "0000000000000000000000000000000000000000000000000000000000000000"),
            (999999, "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"),
        ]

        for attachment_id, sha256 in test_cases:
            # 使用 Step3 函数构建 URI
            uri = generate_attachment_artifact_uri(attachment_id, sha256)

            # 解析 URI
            path = uri.replace("memory://", "")
            parts = path.split("/")

            assert parts[0] == "attachments"
            parsed_id = int(parts[1])
            parsed_sha256 = parts[2]

            # 验证往返一致性
            assert parsed_id == attachment_id
            assert parsed_sha256 == sha256

    def test_attachment_evidence_uri_priority(self):
        """测试 evidence_uri 参数优先级"""
        from step3_chunking import generate_attachment_artifact_uri

        evidence_uri = "memory://attachments/99999/custom_sha256_hash_value"
        uri = generate_attachment_artifact_uri(
            attachment_id=12345,
            sha256="different_hash",
            evidence_uri=evidence_uri,
        )

        assert uri == evidence_uri, "传入的 evidence_uri 应该被优先使用"


# ============================================================
# 测试: ChunkResult 数据结构
# ============================================================

class TestChunkResultDataClass:
    """ChunkResult 数据结构测试"""

    def test_to_dict_complete(self):
        """测试 to_dict 包含所有字段"""
        chunk = ChunkResult(
            chunk_id="test:chunk:id",
            chunk_idx=0,
            content="test content",
            artifact_uri="memory://test/uri",
            sha256="abc123",
            source_id="test:source",
            source_type="test",
            excerpt="excerpt",
            metadata={"key": "value"},
        )

        d = chunk.to_dict()

        assert "chunk_id" in d
        assert "chunk_idx" in d
        assert "content" in d
        assert "artifact_uri" in d
        assert "sha256" in d
        assert "source_id" in d
        assert "source_type" in d
        assert "excerpt" in d
        assert "metadata" in d

        assert d["chunk_id"] == "test:chunk:id"
        assert d["chunk_idx"] == 0
        assert d["metadata"]["key"] == "value"


# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
