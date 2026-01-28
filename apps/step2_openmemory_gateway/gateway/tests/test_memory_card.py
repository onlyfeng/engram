# -*- coding: utf-8 -*-
"""
memory_card 模块单元测试

测试覆盖:
- MemoryCard 创建与验证
- Markdown 生成
- SHA256 计算
- 裁剪逻辑（超长截断、diff/log 处理）
- Evidence 验证
- 便捷函数测试
"""

import hashlib
import pytest

from gateway.memory_card import (
    MemoryCard,
    MemoryKind,
    Confidence,
    Visibility,
    TTL,
    Evidence,
    TrimConfig,
    TrimResult,
    create_memory_card,
    generate_memory_markdown,
    compute_content_sha,
    trim_diff_content,
    trim_log_content,
)


# ======================== Evidence 测试 ========================

class TestEvidence:
    """Evidence 结构测试"""

    def test_valid_evidence(self):
        """有效 Evidence 验证通过"""
        ev = Evidence(
            uri="memory://test/path",
            sha256="a" * 64,
            event_id="evt_001",
        )
        errors = ev.validate()
        assert errors == [], f"不应有验证错误: {errors}"

    def test_valid_evidence_all_schemes(self):
        """所有允许的 URI scheme 验证通过"""
        schemes = ["memory://", "svn://", "git://", "https://"]
        for scheme in schemes:
            ev = Evidence(uri=f"{scheme}test/path", sha256="b" * 64)
            errors = ev.validate()
            assert errors == [], f"scheme {scheme} 应该有效"

    def test_empty_uri_fails(self):
        """空 URI 验证失败"""
        ev = Evidence(uri="", sha256="a" * 64)
        errors = ev.validate()
        assert any("uri 不能为空" in e for e in errors)

    def test_invalid_uri_scheme_fails(self):
        """非法 URI scheme 验证失败"""
        ev = Evidence(uri="http://invalid.com/path", sha256="a" * 64)
        errors = ev.validate()
        assert any("scheme 必须为" in e for e in errors)

    def test_empty_sha256_fails(self):
        """空 SHA256 验证失败"""
        ev = Evidence(uri="memory://test", sha256="")
        errors = ev.validate()
        assert any("sha256 不能为空" in e for e in errors)

    def test_invalid_sha256_format_fails(self):
        """非法 SHA256 格式验证失败"""
        ev = Evidence(uri="memory://test", sha256="invalid_hash")
        errors = ev.validate()
        assert any("sha256 格式无效" in e for e in errors)

    def test_sha256_wrong_length_fails(self):
        """SHA256 长度不对验证失败"""
        ev = Evidence(uri="memory://test", sha256="a" * 32)  # 32位不够
        errors = ev.validate()
        assert any("sha256 格式无效" in e for e in errors)

    def test_to_markdown_lines(self):
        """Markdown 输出格式测试"""
        ev = Evidence(
            uri="git://repo/commit",
            sha256="c" * 64,
            event_id="evt_123",
            git_commit="abc123",
        )
        lines = ev.to_markdown_lines()
        assert "- uri=git://repo/commit" in lines[0]
        assert "sha256=" + "c" * 64 in lines[1]
        assert any("event_id=evt_123" in line for line in lines)
        assert any("git_commit=abc123" in line for line in lines)


# ======================== MemoryCard 测试 ========================

class TestMemoryCard:
    """MemoryCard 结构测试"""

    @pytest.fixture
    def valid_evidence_list(self):
        """有效的证据列表"""
        return [
            Evidence(uri="memory://test/1", sha256="a" * 64),
            Evidence(uri="git://repo/commit", sha256="b" * 64),
        ]

    @pytest.fixture
    def valid_card(self, valid_evidence_list):
        """有效的 MemoryCard"""
        return MemoryCard(
            kind=MemoryKind.PROCEDURE,
            owner="user_001",
            module="backend/api",
            summary="如何处理用户登录请求",
            details=["步骤1：验证参数", "步骤2：调用认证服务", "步骤3：返回 token"],
            evidence=valid_evidence_list,
            confidence=Confidence.HIGH,
            visibility=Visibility.TEAM,
            ttl=TTL.LONG,
        )

    def test_create_valid_card(self, valid_card):
        """创建有效卡片"""
        errors = valid_card.validate()
        assert errors == [], f"不应有验证错误: {errors}"

    def test_card_string_enums_normalization(self, valid_evidence_list):
        """字符串枚举值规范化测试"""
        card = MemoryCard(
            kind="procedure",
            owner="user_001",
            module="test",
            summary="test",
            details=["detail"],
            evidence=valid_evidence_list,
            confidence="high",
            visibility="private",
            ttl="short",
        )
        assert card.kind == MemoryKind.PROCEDURE
        assert card.confidence == Confidence.HIGH
        assert card.visibility == Visibility.PRIVATE
        assert card.ttl == TTL.SHORT

    def test_missing_owner_fails(self, valid_evidence_list):
        """缺少 owner 验证失败"""
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="",
            module="test",
            summary="test",
            details=["detail"],
            evidence=valid_evidence_list,
        )
        errors = card.validate()
        assert any("owner 不能为空" in e for e in errors)

    def test_missing_module_fails(self, valid_evidence_list):
        """缺少 module 验证失败"""
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="",
            summary="test",
            details=["detail"],
            evidence=valid_evidence_list,
        )
        errors = card.validate()
        assert any("module 不能为空" in e for e in errors)

    def test_missing_summary_fails(self, valid_evidence_list):
        """缺少 summary 验证失败"""
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="",
            details=["detail"],
            evidence=valid_evidence_list,
        )
        errors = card.validate()
        assert any("summary 不能为空" in e for e in errors)

    def test_empty_details_fails(self, valid_evidence_list):
        """空 details 验证失败"""
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="test",
            details=[],
            evidence=valid_evidence_list,
        )
        errors = card.validate()
        assert any("details 不能为空" in e for e in errors)

    def test_empty_evidence_fails(self):
        """空 evidence 验证失败"""
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="test",
            details=["detail"],
            evidence=[],
        )
        errors = card.validate()
        assert any("evidence 不能为空" in e for e in errors)

    def test_invalid_evidence_propagates(self):
        """无效 evidence 错误传播"""
        invalid_ev = Evidence(uri="", sha256="invalid")
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="test",
            details=["detail"],
            evidence=[invalid_ev],
        )
        errors = card.validate()
        assert any("evidence[0]" in e for e in errors)

    def test_to_markdown_format(self, valid_card):
        """Markdown 输出格式测试"""
        md = valid_card.to_markdown()
        
        # 检查元数据头
        assert "[Kind] PROCEDURE" in md
        assert "[Owner] user_001" in md
        assert "[Module] backend/api" in md
        assert "[Visibility] team" in md
        assert "[TTL] long" in md
        assert "[Confidence] high" in md
        
        # 检查 Summary
        assert "[Summary]" in md
        assert "如何处理用户登录请求" in md
        
        # 检查 Details
        assert "[Details]" in md
        assert "1) 步骤1：验证参数" in md
        assert "2) 步骤2：调用认证服务" in md
        assert "3) 步骤3：返回 token" in md
        
        # 检查 Evidence
        assert "[Evidence]" in md
        assert "uri=memory://test/1" in md

    def test_compute_payload_sha(self, valid_card):
        """SHA256 计算测试"""
        sha = valid_card.compute_payload_sha()
        
        # 验证格式
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)
        
        # 验证一致性
        sha2 = valid_card.compute_payload_sha()
        assert sha == sha2

    def test_sha_changes_with_content(self, valid_evidence_list):
        """内容变化导致 SHA 变化"""
        card1 = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="summary 1",
            details=["detail"],
            evidence=valid_evidence_list,
        )
        card2 = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="summary 2",
            details=["detail"],
            evidence=valid_evidence_list,
        )
        assert card1.compute_payload_sha() != card2.compute_payload_sha()


# ======================== 裁剪逻辑测试 ========================

class TestTrimming:
    """裁剪逻辑测试"""

    @pytest.fixture
    def evidence(self):
        return [Evidence(uri="memory://test", sha256="a" * 64)]

    def test_summary_trimming(self, evidence):
        """超长 Summary 裁剪"""
        long_summary = "x" * 300  # 超过默认 200 字符限制
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary=long_summary,
            details=["detail"],
            evidence=evidence,
        )
        md = card.to_markdown()
        
        # Summary 应被截断
        logs = card.get_trim_logs()
        assert any("summary 超长截断" in log for log in logs)
        assert "[内容已截断]" in md

    def test_detail_trimming(self, evidence):
        """超长 Detail 裁剪"""
        long_detail = "y" * 600  # 超过默认 500 字符限制
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="test",
            details=[long_detail],
            evidence=evidence,
        )
        md = card.to_markdown()
        
        logs = card.get_trim_logs()
        assert any("details[0] 超长截断" in log for log in logs)

    def test_details_count_limit(self, evidence):
        """Details 条目数限制"""
        many_details = [f"detail {i}" for i in range(20)]  # 超过默认 10 条限制
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="test",
            details=many_details,
            evidence=evidence,
        )
        md = card.to_markdown()
        
        logs = card.get_trim_logs()
        assert any("details 条目数截断" in log for log in logs)
        # 应该只有 10 条 detail
        assert "11) detail 10" not in md

    def test_evidence_count_limit(self):
        """Evidence 条目数限制"""
        many_evidence = [
            Evidence(uri=f"memory://test/{i}", sha256="a" * 64)
            for i in range(15)
        ]
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="test",
            details=["detail"],
            evidence=many_evidence,
        )
        md = card.to_markdown()
        
        logs = card.get_trim_logs()
        assert any("evidence 条目数截断" in log for log in logs)

    def test_diff_content_detection(self, evidence):
        """diff 内容检测与替换"""
        diff_content = """diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,3 +1,4 @@
+import os
 import sys
"""
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="test",
            details=[diff_content],
            evidence=evidence,
        )
        md = card.to_markdown()
        
        logs = card.get_trim_logs()
        assert any("diff 内容替换为指针" in log for log in logs)
        assert "[diff 内容已移除，仅保留指针]" in md

    def test_log_content_detection(self, evidence):
        """log 内容检测与替换"""
        log_content = """[2024-01-01 12:00:00] INFO Starting server
[2024-01-01 12:00:01] DEBUG Loading config
[2024-01-01 12:00:02] ERROR Connection failed"""
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="test",
            details=[log_content],
            evidence=evidence,
        )
        md = card.to_markdown()
        
        logs = card.get_trim_logs()
        assert any("log 内容替换为指针" in log for log in logs)

    def test_custom_trim_config(self, evidence):
        """自定义裁剪配置"""
        config = TrimConfig(
            max_summary_length=50,
            max_detail_length=100,
            max_details_count=3,
        )
        card = MemoryCard(
            kind=MemoryKind.FACT,
            owner="user",
            module="test",
            summary="x" * 100,  # 超过 50
            details=["a" * 50, "b" * 50, "c" * 50, "d" * 50],  # 4 条超过 3 条
            evidence=evidence,
        )
        card._trim_config = config
        md = card.to_markdown()
        
        logs = card.get_trim_logs()
        assert any("summary 超长截断" in log for log in logs)
        assert any("details 条目数截断" in log for log in logs)


# ======================== 便捷函数测试 ========================

class TestConvenienceFunctions:
    """便捷函数测试"""

    def test_create_memory_card(self):
        """create_memory_card 便捷函数"""
        card = create_memory_card(
            kind="FACT",
            owner="user_001",
            module="test/module",
            summary="Test summary",
            details=["Detail 1", "Detail 2"],
            evidence=[
                {"uri": "memory://test", "sha256": "a" * 64},
            ],
            confidence="high",
            visibility="private",
            ttl="short",
        )
        
        assert card.kind == MemoryKind.FACT
        assert card.owner == "user_001"
        assert len(card.evidence) == 1
        assert card.confidence == Confidence.HIGH

    def test_generate_memory_markdown(self):
        """generate_memory_markdown 便捷函数"""
        md, sha = generate_memory_markdown(
            kind="PROCEDURE",
            owner="user_001",
            module="test",
            summary="Test",
            details=["Step 1"],
            evidence=[
                {"uri": "memory://test", "sha256": "b" * 64},
            ],
        )
        
        assert isinstance(md, str)
        assert len(md) > 0
        assert "[Kind] PROCEDURE" in md
        
        assert isinstance(sha, str)
        assert len(sha) == 64

    def test_compute_content_sha(self):
        """compute_content_sha 函数"""
        content = "Hello, World!"
        sha = compute_content_sha(content)
        
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert sha == expected

    def test_trim_diff_content(self):
        """trim_diff_content 函数"""
        diff = "diff --git a/x.py b/x.py\n+new line"
        trimmed, original_sha = trim_diff_content(diff)
        
        assert "[diff 内容已移除，仅保留指针]" in trimmed
        assert original_sha == compute_content_sha(diff)

    def test_trim_diff_content_with_uri(self):
        """trim_diff_content 带 URI"""
        diff = "diff content"
        uri = "svn://repo/r123"
        trimmed, _ = trim_diff_content(diff, uri)
        
        assert uri in trimmed

    def test_trim_log_content(self):
        """trim_log_content 函数"""
        log = "[INFO] Server started\n[DEBUG] Loading"
        trimmed, original_sha = trim_log_content(log)
        
        assert "[log 内容已移除，仅保留指针]" in trimmed
        assert original_sha == compute_content_sha(log)


# ======================== 枚举类型测试 ========================

class TestEnums:
    """枚举类型测试"""

    def test_memory_kind_values(self):
        """MemoryKind 枚举值"""
        assert MemoryKind.FACT.value == "FACT"
        assert MemoryKind.PROCEDURE.value == "PROCEDURE"
        assert MemoryKind.PITFALL.value == "PITFALL"
        assert MemoryKind.DECISION.value == "DECISION"
        assert MemoryKind.REVIEW_GUIDE.value == "REVIEW_GUIDE"
        assert MemoryKind.REFLECTION.value == "REFLECTION"

    def test_confidence_values(self):
        """Confidence 枚举值"""
        assert Confidence.HIGH.value == "high"
        assert Confidence.MID.value == "mid"
        assert Confidence.LOW.value == "low"

    def test_visibility_values(self):
        """Visibility 枚举值"""
        assert Visibility.TEAM.value == "team"
        assert Visibility.PRIVATE.value == "private"
        assert Visibility.ORG.value == "org"

    def test_ttl_values(self):
        """TTL 枚举值"""
        assert TTL.LONG.value == "long"
        assert TTL.MID.value == "mid"
        assert TTL.SHORT.value == "short"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
