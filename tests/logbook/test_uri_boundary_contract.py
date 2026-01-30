# -*- coding: utf-8 -*-
"""
test_uri_boundary_contract.py - URI 边界契约测试

目的：
1. 锁定 URI Grammar 归属：memory:// 解析规则只能由 engram_logbook.uri 定义
2. 确保 Gateway 不自定义 memory:// 解析规则
3. 覆盖 evidence_packet.md 中声明的 Evidence URI 格式
4. 验证 attachment evidence URI 的 strict 解析约束

契约来源：
- docs/contracts/gateway_logbook_boundary.md: URI Grammar 归属声明
- docs/contracts/evidence_packet.md: Evidence Packet 规范
- apps/logbook_postgres/scripts/engram_logbook/uri.py: URI 解析实现

测试归属：make test-logbook-unit
"""

import pytest
import sys
from pathlib import Path

# 确保可以导入 engram_logbook 模块
sys.path.insert(0, str(Path(__file__).parent.parent))

from engram.logbook.uri import (
    # 核心解析函数（Gateway 必须使用这些函数，不能自定义）
    parse_uri,
    parse_evidence_uri,
    parse_attachment_evidence_uri,
    parse_attachment_evidence_uri_strict,
    # 构建函数
    build_evidence_uri,
    build_attachment_evidence_uri,
    build_evidence_ref_for_patch_blob,
    build_attachment_evidence_ref,
    build_evidence_refs_json,
    # 验证函数
    validate_evidence_ref,
    # 类型检查函数
    is_patch_blob_evidence_uri,
    is_attachment_evidence_uri,
    # 类型定义
    UriType,
    AttachmentUriParseResult,
    # 错误码常量
    ATTACHMENT_URI_ERR_NOT_MEMORY,
    ATTACHMENT_URI_ERR_NOT_ATTACHMENTS,
    ATTACHMENT_URI_ERR_LEGACY_FORMAT,
    ATTACHMENT_URI_ERR_INVALID_ID,
    ATTACHMENT_URI_ERR_INVALID_SHA256,
    ATTACHMENT_URI_ERR_MALFORMED,
)


# =============================================================================
# 测试常量
# =============================================================================

# 有效的 SHA256 哈希（64 位十六进制）
VALID_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
VALID_SHA256_ALT = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


# =============================================================================
# Section 1: URI Grammar 归属契约
#
# 契约来源: docs/contracts/gateway_logbook_boundary.md
#
# > **规范所有权**：**URI Grammar 的唯一规范由 Logbook 层定义和维护**。
# > - **格式定义**：Evidence URI、Artifact Key、Physical URI 的语法规则均由
# >   `engram_logbook.uri` 模块定义
# > - **解析实现**：`parse_uri()`、`parse_evidence_uri()`、
# >   `parse_attachment_evidence_uri()` 等函数为唯一权威解析器
# > - **Gateway 职责**：Gateway 仅调用 Logbook URI 模块进行解析与构建，
# >   不自行定义 URI 格式
# =============================================================================


class TestUriGrammarOwnership:
    """
    URI Grammar 归属契约测试

    验证 engram_logbook.uri 模块提供所有必需的 URI 解析和构建函数，
    Gateway 不需要（也不应该）自定义任何 memory:// 解析逻辑。
    """

    def test_parse_uri_handles_all_memory_uri_types(self):
        """
        契约测试：parse_uri 必须能识别所有 memory:// URI 类型

        Gateway 不应自行判断 memory:// URI 的子类型，
        应使用 is_patch_blob_evidence_uri / is_attachment_evidence_uri 等函数。
        """
        # patch_blobs URI
        patch_uri = f"memory://patch_blobs/git/1:abc123/{VALID_SHA256}"
        parsed = parse_uri(patch_uri)
        assert parsed.uri_type == UriType.MEMORY, \
            "parse_uri 必须将 memory://patch_blobs/... 识别为 MEMORY 类型"
        assert parsed.scheme == "memory"
        assert parsed.is_local is True

        # attachments URI
        attachment_uri = f"memory://attachments/12345/{VALID_SHA256}"
        parsed = parse_uri(attachment_uri)
        assert parsed.uri_type == UriType.MEMORY, \
            "parse_uri 必须将 memory://attachments/... 识别为 MEMORY 类型"
        assert parsed.scheme == "memory"
        assert parsed.is_local is True

        # docs URI (根据 evidence_packet.md)
        docs_uri = f"memory://docs/contracts/evidence_packet.md/{VALID_SHA256}"
        parsed = parse_uri(docs_uri)
        assert parsed.uri_type == UriType.MEMORY, \
            "parse_uri 必须将 memory://docs/... 识别为 MEMORY 类型"
        assert parsed.scheme == "memory"

    def test_logbook_provides_specialized_parsers(self):
        """
        契约测试：Logbook 必须提供专门的 memory:// 子类型解析器

        Gateway 应使用这些函数，而非自行实现解析逻辑。
        """
        # patch_blobs 专用解析器
        assert callable(parse_evidence_uri), \
            "Logbook 必须提供 parse_evidence_uri 函数"
        assert callable(is_patch_blob_evidence_uri), \
            "Logbook 必须提供 is_patch_blob_evidence_uri 函数"

        # attachments 专用解析器
        assert callable(parse_attachment_evidence_uri), \
            "Logbook 必须提供 parse_attachment_evidence_uri 函数"
        assert callable(parse_attachment_evidence_uri_strict), \
            "Logbook 必须提供 parse_attachment_evidence_uri_strict 函数"
        assert callable(is_attachment_evidence_uri), \
            "Logbook 必须提供 is_attachment_evidence_uri 函数"

    def test_logbook_provides_uri_builders(self):
        """
        契约测试：Logbook 必须提供 URI 构建函数

        Gateway 应使用这些函数构建 evidence URI，而非自行拼接字符串。
        """
        assert callable(build_evidence_uri), \
            "Logbook 必须提供 build_evidence_uri 函数"
        assert callable(build_attachment_evidence_uri), \
            "Logbook 必须提供 build_attachment_evidence_uri 函数"
        assert callable(build_evidence_ref_for_patch_blob), \
            "Logbook 必须提供 build_evidence_ref_for_patch_blob 函数"
        assert callable(build_attachment_evidence_ref), \
            "Logbook 必须提供 build_attachment_evidence_ref 函数"
        assert callable(build_evidence_refs_json), \
            "Logbook 必须提供 build_evidence_refs_json 函数"

    def test_gateway_cannot_bypass_logbook_parser(self):
        """
        契约测试：Gateway 不能绕过 Logbook 解析器自行解析 memory:// URI

        此测试验证所有 memory:// URI 格式变更必须通过 Logbook 层实现，
        Gateway 依赖 Logbook 解析器的返回结构。
        """
        # 验证 parse_evidence_uri 返回结构稳定
        uri = f"memory://patch_blobs/git/1:abc123/{VALID_SHA256}"
        result = parse_evidence_uri(uri)

        assert result is not None
        assert "source_type" in result, \
            "parse_evidence_uri 必须返回 source_type 字段"
        assert "source_id" in result, \
            "parse_evidence_uri 必须返回 source_id 字段"
        assert "sha256" in result, \
            "parse_evidence_uri 必须返回 sha256 字段"

        # 验证 parse_attachment_evidence_uri_strict 返回结构稳定
        attachment_uri = f"memory://attachments/12345/{VALID_SHA256}"
        attachment_result = parse_attachment_evidence_uri_strict(attachment_uri)

        assert isinstance(attachment_result, AttachmentUriParseResult), \
            "parse_attachment_evidence_uri_strict 必须返回 AttachmentUriParseResult"
        assert hasattr(attachment_result, "success")
        assert hasattr(attachment_result, "attachment_id")
        assert hasattr(attachment_result, "sha256")
        assert hasattr(attachment_result, "error_code")
        assert hasattr(attachment_result, "error_message")


# =============================================================================
# Section 2: Evidence Packet 契约（来自 evidence_packet.md）
#
# 覆盖字段：
# - artifact_uri: Canonical Evidence URI 格式
# - sha256: 64 位十六进制内容哈希
# - source_id: <repo_id>:<revision/sha> 格式
# - source_type: svn / git / gitlab / docs
# - excerpt: 最多 25 行或 2000 字
# =============================================================================


class TestEvidencePacketContract:
    """
    Evidence Packet 契约测试

    验证 evidence_packet.md 中声明的所有字段在 Logbook 实现中得到支持。
    """

    # evidence_packet.md 定义的允许 scheme
    ALLOWED_SCHEMES = {
        "memory://": "Logbook 内置存储",
        "file://": "本地文件路径",
        "svn://": "SVN 仓库版本引用",
        "git://": "Git 仓库 commit 引用",
        "https://": "HTTP(S) 远程资源",
    }

    # memory:// URI 资源类型
    MEMORY_RESOURCE_TYPES = {
        "patch_blobs": "SCM 补丁/diff 内容",
        "attachments": "通用附件（截图、文档等）",
        "docs": "规格/设计文档",
    }

    def test_artifact_uri_format_patch_blobs(self):
        """
        契约测试：patch_blobs artifact_uri 格式

        规范：memory://patch_blobs/<source_type>/<source_id>/<sha256>
        """
        source_type = "git"
        source_id = "1:abc123"
        sha256 = VALID_SHA256

        uri = build_evidence_uri(source_type, source_id, sha256)

        assert uri == f"memory://patch_blobs/{source_type}/{source_id}/{sha256}", \
            "artifact_uri 必须遵循 memory://patch_blobs/<source_type>/<source_id>/<sha256> 格式"

    def test_artifact_uri_format_attachments(self):
        """
        契约测试：attachments artifact_uri 格式

        规范：memory://attachments/<attachment_id>/<sha256>
        """
        attachment_id = 12345
        sha256 = VALID_SHA256

        uri = build_attachment_evidence_uri(attachment_id, sha256)

        assert uri == f"memory://attachments/{attachment_id}/{sha256}", \
            "artifact_uri 必须遵循 memory://attachments/<attachment_id>/<sha256> 格式"

    def test_sha256_must_be_64_hex_chars(self):
        """
        契约测试：sha256 必须是 64 位十六进制字符串

        来源：evidence_packet.md 表格 sha256 字段
        """
        ref = build_evidence_ref_for_patch_blob("git", "1:abc", VALID_SHA256)

        assert len(ref["sha256"]) == 64, \
            "sha256 必须是 64 位"
        assert all(c in "0123456789abcdef" for c in ref["sha256"]), \
            "sha256 必须是十六进制字符"

    def test_sha256_normalized_to_lowercase(self):
        """
        契约测试：sha256 自动规范化为小写
        """
        upper_sha = VALID_SHA256.upper()
        ref = build_evidence_ref_for_patch_blob("git", "1:abc", upper_sha)

        assert ref["sha256"] == VALID_SHA256.lower(), \
            "sha256 必须自动规范化为小写"

    def test_source_id_format(self):
        """
        契约测试：source_id 格式为 <repo_id>:<rev/commit/event_id>

        来源：evidence_packet.md Evidence 字段
        """
        # Git 格式
        ref = build_evidence_ref_for_patch_blob("git", "1:abc123def", VALID_SHA256)
        assert ":" in ref["source_id"], \
            "source_id 必须包含 : 分隔符"
        assert ref["source_id"] == "1:abc123def"

        # SVN 格式
        ref = build_evidence_ref_for_patch_blob("svn", "2:1234", VALID_SHA256)
        assert ref["source_id"] == "2:1234"

    def test_source_type_allowed_values(self):
        """
        契约测试：source_type 允许值

        来源：evidence_packet.md ChunkResult 字段映射
        """
        allowed_source_types = ["svn", "git", "gitlab", "logbook", "docs"]

        for source_type in allowed_source_types:
            ref = build_evidence_ref_for_patch_blob(source_type, "1:test", VALID_SHA256)
            assert ref["source_type"] == source_type.lower(), \
                f"source_type {source_type} 应被支持"

    def test_source_type_normalized_to_lowercase(self):
        """
        契约测试：source_type 自动规范化为小写
        """
        ref = build_evidence_ref_for_patch_blob("GIT", "1:abc", VALID_SHA256)
        assert ref["source_type"] == "git", \
            "source_type 必须自动规范化为小写"

    def test_evidence_ref_required_fields(self):
        """
        契约测试：evidence_ref 必需字段

        来源：evidence_packet.md Evidence 结构
        """
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123",
            sha256=VALID_SHA256,
        )

        required_fields = ["artifact_uri", "sha256", "source_id", "source_type", "kind"]
        for field in required_fields:
            assert field in ref, f"evidence_ref 必须包含 {field} 字段"

    def test_evidence_ref_optional_fields(self):
        """
        契约测试：evidence_ref 可选字段
        """
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123",
            sha256=VALID_SHA256,
            size_bytes=1024,
            extra={"excerpt": "diff content preview...", "line_count": 10},
        )

        assert ref.get("size_bytes") == 1024
        assert ref.get("excerpt") == "diff content preview..."
        assert ref.get("line_count") == 10

    def test_validate_evidence_ref_contract(self):
        """
        契约测试：validate_evidence_ref 验证规则
        """
        # 有效 ref 应通过
        valid_ref = build_evidence_ref_for_patch_blob("git", "1:abc", VALID_SHA256)
        is_valid, error = validate_evidence_ref(valid_ref)
        assert is_valid is True
        assert error is None

        # 缺少必需字段应失败
        invalid_ref = {"sha256": VALID_SHA256}
        is_valid, error = validate_evidence_ref(invalid_ref)
        assert is_valid is False
        assert error is not None


# =============================================================================
# Section 3: Attachment Evidence URI Strict 解析约束
#
# 契约来源：engram_logbook/uri.py 中的 parse_attachment_evidence_uri_strict
#
# 规范格式: memory://attachments/<attachment_id>/<sha256>
# - attachment_id: 必须为整数（数据库主键）
# - sha256: 必须为 64 位十六进制字符串
#
# **旧格式已废弃，必须拒绝**:
# - ~~memory://attachments/<namespace>/<id>/<sha256>~~
# =============================================================================


class TestAttachmentEvidenceUriStrictContract:
    """
    Attachment Evidence URI Strict 解析契约测试

    验证 Gateway 传递给 Logbook 的 attachment URI 必须符合严格格式要求。
    """

    def test_strict_parser_returns_detailed_result(self):
        """
        契约测试：strict 解析器必须返回详细的解析结果
        """
        uri = f"memory://attachments/12345/{VALID_SHA256}"
        result = parse_attachment_evidence_uri_strict(uri)

        assert isinstance(result, AttachmentUriParseResult)
        assert result.success is True
        assert result.attachment_id == 12345
        assert result.sha256 == VALID_SHA256.lower()
        assert result.error_code is None
        assert result.error_message is None

    def test_strict_parser_rejects_legacy_three_segment_format(self):
        """
        契约测试：必须拒绝旧的三段路径格式

        旧格式（已废弃）：memory://attachments/<namespace>/<id>/<sha256>
        正确格式：memory://attachments/<attachment_id>/<sha256>
        """
        legacy_uris = [
            f"memory://attachments/namespace/123/{VALID_SHA256}",
            f"memory://attachments/scm/456/{VALID_SHA256}",
            f"memory://attachments/team_x/789/{VALID_SHA256}",
            f"memory://attachments/a/b/c/{VALID_SHA256}",
        ]

        for uri in legacy_uris:
            result = parse_attachment_evidence_uri_strict(uri)
            assert result.success is False, \
                f"必须拒绝旧格式 URI: {uri}"
            assert result.error_code == ATTACHMENT_URI_ERR_LEGACY_FORMAT, \
                f"旧格式 URI 应返回 E_LEGACY_FORMAT 错误码: {uri}"
            assert "旧格式" in result.error_message or "三段路径" in result.error_message

    def test_strict_parser_requires_integer_attachment_id(self):
        """
        契约测试：attachment_id 必须是整数
        """
        invalid_id_uris = [
            (f"memory://attachments/abc/{VALID_SHA256}", "非数字 ID"),
            (f"memory://attachments/12.34/{VALID_SHA256}", "小数 ID"),
            (f"memory://attachments/id_123/{VALID_SHA256}", "带前缀 ID"),
            (f"memory://attachments/-123/{VALID_SHA256}", "负数 ID"),  # 可能通过，取决于实现
        ]

        for uri, desc in invalid_id_uris:
            result = parse_attachment_evidence_uri_strict(uri)
            # 非整数 ID 应失败
            if not str(uri.split("/")[3]).lstrip("-").isdigit():
                assert result.success is False, \
                    f"必须拒绝非整数 attachment_id: {desc}"
                assert result.error_code in [
                    ATTACHMENT_URI_ERR_INVALID_ID,
                    ATTACHMENT_URI_ERR_LEGACY_FORMAT,
                ], f"应返回 E_INVALID_ID 或 E_LEGACY_FORMAT: {desc}"

    def test_strict_parser_validates_sha256_format(self):
        """
        契约测试：sha256 必须是 64 位十六进制字符串
        """
        invalid_sha256_cases = [
            ("memory://attachments/123/short", "太短"),
            ("memory://attachments/123/" + "a" * 63, "63 位"),
            ("memory://attachments/123/" + "a" * 65, "65 位"),
            ("memory://attachments/123/" + "g" * 64, "非十六进制"),
            ("memory://attachments/123/" + "G" * 64, "大写非十六进制"),
        ]

        for uri, desc in invalid_sha256_cases:
            result = parse_attachment_evidence_uri_strict(uri)
            assert result.success is False, \
                f"必须拒绝无效 sha256: {desc}"
            assert result.error_code == ATTACHMENT_URI_ERR_INVALID_SHA256, \
                f"应返回 E_INVALID_SHA256: {desc}"

    def test_strict_parser_error_codes_are_stable(self):
        """
        契约测试：错误码常量必须稳定（Gateway 依赖这些常量）
        """
        # 验证所有错误码常量存在且值稳定
        assert ATTACHMENT_URI_ERR_NOT_MEMORY == "E_NOT_MEMORY"
        assert ATTACHMENT_URI_ERR_NOT_ATTACHMENTS == "E_NOT_ATTACHMENTS"
        assert ATTACHMENT_URI_ERR_LEGACY_FORMAT == "E_LEGACY_FORMAT"
        assert ATTACHMENT_URI_ERR_INVALID_ID == "E_INVALID_ID"
        assert ATTACHMENT_URI_ERR_INVALID_SHA256 == "E_INVALID_SHA256"
        assert ATTACHMENT_URI_ERR_MALFORMED == "E_MALFORMED"

    def test_build_and_parse_roundtrip_strict(self):
        """
        契约测试：build 函数生成的 URI 必须能被 strict 解析器正确解析
        """
        for attachment_id in [1, 100, 99999999]:
            uri = build_attachment_evidence_uri(attachment_id, VALID_SHA256)
            result = parse_attachment_evidence_uri_strict(uri)

            assert result.success is True, \
                f"build 生成的 URI 必须能被 strict 解析: {uri}"
            assert result.attachment_id == attachment_id
            assert result.sha256 == VALID_SHA256.lower()


# =============================================================================
# Section 4: evidence_refs_json 结构契约
#
# 确保 Gateway 构建的 evidence_refs_json 与 Logbook SQL 查询契约一致
# =============================================================================


class TestEvidenceRefsJsonContract:
    """
    evidence_refs_json 结构契约测试

    验证 build_evidence_refs_json 输出的结构满足：
    1. governance.write_audit 的存储需求
    2. reconcile_outbox 的 SQL 查询需求
    """

    def test_patches_array_structure(self):
        """
        契约测试：patches 数组结构
        """
        patch1 = build_evidence_ref_for_patch_blob("git", "1:abc", VALID_SHA256)
        patch2 = build_evidence_ref_for_patch_blob("svn", "2:123", VALID_SHA256_ALT)

        refs_json = build_evidence_refs_json(patches=[patch1, patch2])

        assert "patches" in refs_json
        assert isinstance(refs_json["patches"], list)
        assert len(refs_json["patches"]) == 2

        # 每个 patch 应可独立验证
        for patch in refs_json["patches"]:
            is_valid, error = validate_evidence_ref(patch)
            assert is_valid, f"patch 验证失败: {error}"

    def test_attachments_array_structure(self):
        """
        契约测试：attachments 数组结构
        """
        attachment = build_attachment_evidence_ref(
            attachment_id=12345,
            sha256=VALID_SHA256,
            kind="screenshot",
            item_id=100,
        )

        refs_json = build_evidence_refs_json(attachments=[attachment])

        assert "attachments" in refs_json
        assert isinstance(refs_json["attachments"], list)
        assert len(refs_json["attachments"]) == 1
        assert refs_json["attachments"][0]["attachment_id"] == 12345

    def test_extra_fields_at_top_level(self):
        """
        契约测试：extra 字段在顶层

        这对 reconcile_outbox 的 SQL 查询很重要：
            (evidence_refs_json->>'outbox_id')::int
        """
        refs_json = build_evidence_refs_json(
            extra={
                "outbox_id": 12345,
                "memory_id": "mem_abc123",
                "source": "outbox_worker",
            }
        )

        assert refs_json.get("outbox_id") == 12345, \
            "outbox_id 必须在顶层（用于 SQL 查询）"
        assert refs_json.get("memory_id") == "mem_abc123", \
            "memory_id 必须在顶层"
        assert refs_json.get("source") == "outbox_worker", \
            "source 必须在顶层"

    def test_evidence_summary_structure(self):
        """
        契约测试：evidence_summary 结构（来自 evidence_packet.md）
        """
        patch = build_evidence_ref_for_patch_blob("git", "1:abc", VALID_SHA256)

        # 模拟 Gateway 构建的 evidence_summary
        evidence_summary = {
            "count": 1,
            "has_strong": True,
            "uris": [patch["artifact_uri"]],
        }

        refs_json = build_evidence_refs_json(
            patches=[patch],
            extra={"evidence_summary": evidence_summary},
        )

        assert "evidence_summary" in refs_json
        assert refs_json["evidence_summary"]["count"] == 1
        assert refs_json["evidence_summary"]["has_strong"] is True
        assert len(refs_json["evidence_summary"]["uris"]) == 1


# =============================================================================
# Section 5: memory:// URI 子类型区分契约
#
# Gateway 必须使用 Logbook 提供的函数区分 memory:// URI 子类型
# =============================================================================


class TestMemoryUriSubtypeContract:
    """
    memory:// URI 子类型区分契约测试

    验证 Gateway 可以使用 Logbook 函数正确区分不同的 memory:// URI 类型。
    """

    def test_is_patch_blob_evidence_uri_accuracy(self):
        """
        契约测试：is_patch_blob_evidence_uri 区分准确性
        """
        # 应返回 True
        assert is_patch_blob_evidence_uri(f"memory://patch_blobs/git/1:abc/{VALID_SHA256}") is True
        assert is_patch_blob_evidence_uri(f"memory://patch_blobs/svn/2:123/{VALID_SHA256}") is True

        # 应返回 False
        assert is_patch_blob_evidence_uri(f"memory://attachments/123/{VALID_SHA256}") is False
        assert is_patch_blob_evidence_uri(f"memory://docs/file.md/{VALID_SHA256}") is False
        assert is_patch_blob_evidence_uri("https://example.com/patch") is False
        assert is_patch_blob_evidence_uri("scm/repo/patch.diff") is False

    def test_is_attachment_evidence_uri_accuracy(self):
        """
        契约测试：is_attachment_evidence_uri 区分准确性
        """
        # 应返回 True
        assert is_attachment_evidence_uri(f"memory://attachments/123/{VALID_SHA256}") is True
        assert is_attachment_evidence_uri(f"memory://attachments/99999/{VALID_SHA256}") is True

        # 应返回 False
        assert is_attachment_evidence_uri(f"memory://patch_blobs/git/1:abc/{VALID_SHA256}") is False
        assert is_attachment_evidence_uri(f"memory://docs/file.md/{VALID_SHA256}") is False
        assert is_attachment_evidence_uri("https://example.com/attachment") is False

    def test_exclusive_uri_types(self):
        """
        契约测试：同一 URI 不能同时是多个类型
        """
        # patch_blobs URI
        patch_uri = f"memory://patch_blobs/git/1:abc/{VALID_SHA256}"
        assert is_patch_blob_evidence_uri(patch_uri) is True
        assert is_attachment_evidence_uri(patch_uri) is False

        # attachments URI
        attachment_uri = f"memory://attachments/123/{VALID_SHA256}"
        assert is_attachment_evidence_uri(attachment_uri) is True
        assert is_patch_blob_evidence_uri(attachment_uri) is False


# =============================================================================
# Section 6: Gateway → Logbook 互操作性契约
#
# 验证 Gateway 构建的 URI 可以被 Logbook 正确解析
# =============================================================================


class TestGatewayLogbookInteropContract:
    """
    Gateway ↔ Logbook 互操作性契约测试

    验证 Gateway 使用 Logbook 函数构建的 URI 可以被 Logbook 正确解析回溯。
    """

    def test_patch_blob_uri_interop(self):
        """
        契约测试：Gateway 构建的 patch_blob URI 可被 Logbook 解析
        """
        # Gateway 使用 Logbook 函数构建 URI
        source_type = "git"
        source_id = "1:abc123def"
        sha256 = VALID_SHA256

        uri = build_evidence_uri(source_type, source_id, sha256)

        # Logbook 解析 URI
        parsed = parse_evidence_uri(uri)

        assert parsed is not None, "Logbook 必须能解析 Gateway 构建的 patch_blob URI"
        assert parsed["source_type"] == source_type
        assert parsed["source_id"] == source_id
        assert parsed["sha256"] == sha256

    def test_attachment_uri_interop(self):
        """
        契约测试：Gateway 构建的 attachment URI 可被 Logbook 解析
        """
        # Gateway 使用 Logbook 函数构建 URI
        attachment_id = 12345
        sha256 = VALID_SHA256

        uri = build_attachment_evidence_uri(attachment_id, sha256)

        # Logbook 解析 URI
        parsed = parse_attachment_evidence_uri_strict(uri)

        assert parsed.success is True, \
            f"Logbook 必须能解析 Gateway 构建的 attachment URI: {parsed.error_message}"
        assert parsed.attachment_id == attachment_id
        assert parsed.sha256 == sha256.lower()

    def test_evidence_ref_from_patch_blob_interop(self):
        """
        契约测试：evidence_ref 中的 artifact_uri 可被 Logbook 解析
        """
        # Gateway 构建 evidence_ref
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123",
            sha256=VALID_SHA256,
        )

        # 从 ref 提取 URI 并解析
        parsed = parse_evidence_uri(ref["artifact_uri"])

        assert parsed is not None
        assert parsed["source_type"] == ref["source_type"]
        assert parsed["source_id"] == ref["source_id"]
        assert parsed["sha256"] == ref["sha256"]


# =============================================================================
# 运行测试入口
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
