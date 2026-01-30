# -*- coding: utf-8 -*-
"""
test_scm_sync_keys.py - SCM 同步键名规范化模块单元测试

测试:
- normalize_instance_key: 大小写归一化、端口归一化、协议归一化
- extract_tenant_id: 从 payload_json 和 project_key 提取租户 ID
- extract_instance_key: 从 payload_json 和 URL 提取实例标识
- 边界情况：空值、None、特殊字符
"""

import pytest

from engram.logbook.scm_sync_keys import (
    normalize_instance_key,
    extract_tenant_id,
    extract_instance_key,
    extract_instance_and_tenant,
)


class TestNormalizeInstanceKey:
    """normalize_instance_key 函数测试"""

    # === 基本功能测试 ===
    
    def test_simple_host(self):
        """简单 host 保持不变（转小写）"""
        assert normalize_instance_key("gitlab.example.com") == "gitlab.example.com"

    def test_https_url(self):
        """HTTPS URL 提取 host"""
        assert normalize_instance_key("https://gitlab.example.com/group/project") == "gitlab.example.com"

    def test_http_url(self):
        """HTTP URL 提取 host"""
        assert normalize_instance_key("http://gitlab.local/repo") == "gitlab.local"

    # === 大小写归一化 ===
    
    def test_uppercase_host(self):
        """大写 host 转小写"""
        assert normalize_instance_key("GITLAB.EXAMPLE.COM") == "gitlab.example.com"

    def test_mixed_case_host(self):
        """混合大小写 host 转小写"""
        assert normalize_instance_key("GitLab.Example.COM") == "gitlab.example.com"

    def test_uppercase_url(self):
        """大写 URL 转小写"""
        assert normalize_instance_key("HTTPS://GITLAB.CORP.COM/Group/Project") == "gitlab.corp.com"

    # === 端口归一化 ===
    
    def test_port_443_removed(self):
        """默认 HTTPS 端口 443 被移除"""
        assert normalize_instance_key("gitlab.example.com:443") == "gitlab.example.com"
        assert normalize_instance_key("https://gitlab.example.com:443/") == "gitlab.example.com"

    def test_port_80_removed(self):
        """默认 HTTP 端口 80 被移除"""
        assert normalize_instance_key("gitlab.local:80") == "gitlab.local"
        assert normalize_instance_key("http://gitlab.local:80/repo") == "gitlab.local"

    def test_custom_port_preserved(self):
        """自定义端口保留"""
        assert normalize_instance_key("gitlab.local:8080") == "gitlab.local:8080"
        assert normalize_instance_key("https://gitlab.local:8443/") == "gitlab.local:8443"

    def test_port_8080(self):
        """常见自定义端口 8080"""
        assert normalize_instance_key("http://gitlab.local:8080/repo") == "gitlab.local:8080"

    # === 协议归一化 ===
    
    def test_https_protocol_ignored(self):
        """HTTPS 协议被忽略，只保留 host"""
        result = normalize_instance_key("https://gitlab.example.com/path")
        assert result == "gitlab.example.com"
        assert "https" not in result

    def test_http_protocol_ignored(self):
        """HTTP 协议被忽略，只保留 host"""
        result = normalize_instance_key("http://gitlab.example.com/path")
        assert result == "gitlab.example.com"
        assert "http" not in result

    def test_different_protocols_same_result(self):
        """HTTP 和 HTTPS 同一 host 结果相同"""
        http_result = normalize_instance_key("http://gitlab.example.com/")
        https_result = normalize_instance_key("https://gitlab.example.com/")
        assert http_result == https_result == "gitlab.example.com"

    # === 路径处理 ===
    
    def test_path_ignored(self):
        """URL 路径被忽略"""
        assert normalize_instance_key("https://gitlab.example.com/group/subgroup/project.git") == "gitlab.example.com"

    def test_trailing_slash(self):
        """尾部斜杠不影响结果"""
        assert normalize_instance_key("https://gitlab.example.com/") == "gitlab.example.com"

    def test_host_with_path_no_protocol(self):
        """无协议的 host 带路径"""
        assert normalize_instance_key("gitlab.example.com/group/project") == "gitlab.example.com"

    # === 空值和边界情况 ===
    
    def test_none_returns_none(self):
        """None 输入返回 None"""
        assert normalize_instance_key(None) is None

    def test_empty_string_returns_none(self):
        """空字符串返回 None"""
        assert normalize_instance_key("") is None

    def test_whitespace_only_returns_none(self):
        """仅空白字符返回 None"""
        assert normalize_instance_key("   ") is None

    def test_whitespace_trimmed(self):
        """前后空白被去除"""
        assert normalize_instance_key("  gitlab.example.com  ") == "gitlab.example.com"

    def test_protocol_only_returns_none(self):
        """仅协议返回 None"""
        assert normalize_instance_key("https://") is None

    # === 复杂 URL 场景 ===
    
    def test_url_with_auth(self):
        """带认证信息的 URL（@前的部分是认证信息）"""
        # urlparse 会将认证信息放在 username/password，netloc 包含 user@host
        result = normalize_instance_key("https://user:pass@gitlab.example.com/repo")
        # netloc 是 "user:pass@gitlab.example.com"
        assert "gitlab.example.com" in result

    def test_ip_address(self):
        """IP 地址作为 host"""
        assert normalize_instance_key("http://192.168.1.100/repo") == "192.168.1.100"
        assert normalize_instance_key("192.168.1.100") == "192.168.1.100"

    def test_ip_with_port(self):
        """IP 地址带端口"""
        assert normalize_instance_key("http://192.168.1.100:8080/") == "192.168.1.100:8080"

    def test_localhost(self):
        """localhost 保持不变"""
        assert normalize_instance_key("localhost") == "localhost"
        assert normalize_instance_key("http://localhost:3000/") == "localhost:3000"


class TestExtractTenantId:
    """extract_tenant_id 函数测试"""

    # === 从 payload_json 提取 ===
    
    def test_payload_tenant_id(self):
        """从 payload_json 直接提取 tenant_id"""
        payload = {"tenant_id": "acme"}
        assert extract_tenant_id(payload_json=payload) == "acme"

    def test_payload_tenant_id_priority(self):
        """payload_json 的 tenant_id 优先于 project_key"""
        payload = {"tenant_id": "from_payload"}
        project_key = "from_project/something"
        assert extract_tenant_id(payload_json=payload, project_key=project_key) == "from_payload"

    def test_payload_tenant_id_with_whitespace(self):
        """tenant_id 前后空白被去除"""
        payload = {"tenant_id": "  acme  "}
        assert extract_tenant_id(payload_json=payload) == "acme"

    # === 从 project_key 提取 ===
    
    def test_project_key_with_slash(self):
        """从 project_key 提取 / 前的部分"""
        assert extract_tenant_id(project_key="tenant-a/project-x") == "tenant-a"

    def test_project_key_multiple_slashes(self):
        """多级路径只取第一级"""
        assert extract_tenant_id(project_key="org/team/project") == "org"

    def test_project_key_no_slash(self):
        """无 / 的 project_key 返回 None"""
        assert extract_tenant_id(project_key="single_project") is None

    def test_project_key_empty_tenant(self):
        """/ 前为空返回 None"""
        assert extract_tenant_id(project_key="/project") is None

    def test_project_key_whitespace_trimmed(self):
        """project_key 空白被去除"""
        assert extract_tenant_id(project_key="  tenant/project  ") == "tenant"

    # === 空值和边界情况 ===
    
    def test_none_payload_none_project(self):
        """两个参数都是 None 返回 None"""
        assert extract_tenant_id(None, None) is None

    def test_empty_payload(self):
        """空 payload 返回 None"""
        assert extract_tenant_id(payload_json={}) is None

    def test_payload_empty_tenant_id(self):
        """payload 中 tenant_id 为空字符串时使用 project_key"""
        payload = {"tenant_id": ""}
        assert extract_tenant_id(payload_json=payload, project_key="tenant/proj") == "tenant"

    def test_payload_whitespace_tenant_id(self):
        """payload 中 tenant_id 仅空白时使用 project_key"""
        payload = {"tenant_id": "   "}
        assert extract_tenant_id(payload_json=payload, project_key="tenant/proj") == "tenant"

    def test_payload_non_string_tenant_id(self):
        """payload 中 tenant_id 非字符串时使用 project_key"""
        payload = {"tenant_id": 123}
        assert extract_tenant_id(payload_json=payload, project_key="tenant/proj") == "tenant"

    def test_payload_none_tenant_id(self):
        """payload 中 tenant_id 为 None 时使用 project_key"""
        payload = {"tenant_id": None}
        assert extract_tenant_id(payload_json=payload, project_key="tenant/proj") == "tenant"

    def test_empty_project_key(self):
        """空 project_key 返回 None"""
        assert extract_tenant_id(project_key="") is None


class TestExtractInstanceKey:
    """extract_instance_key 函数测试"""

    # === 从 payload_json 提取 ===
    
    def test_payload_gitlab_instance(self):
        """从 payload_json 直接提取 gitlab_instance"""
        payload = {"gitlab_instance": "gitlab.example.com"}
        assert extract_instance_key(payload_json=payload) == "gitlab.example.com"

    def test_payload_gitlab_instance_normalized(self):
        """payload 中的 gitlab_instance 会被规范化"""
        payload = {"gitlab_instance": "GITLAB.EXAMPLE.COM:443"}
        assert extract_instance_key(payload_json=payload) == "gitlab.example.com"

    def test_payload_priority(self):
        """payload_json 的 gitlab_instance 优先于 URL"""
        payload = {"gitlab_instance": "primary.gitlab.com"}
        url = "https://secondary.gitlab.com/repo"
        assert extract_instance_key(payload_json=payload, url=url) == "primary.gitlab.com"

    # === 从 URL 提取 ===
    
    def test_url_extraction(self):
        """从 URL 提取实例标识"""
        assert extract_instance_key(url="https://gitlab.corp.com/group/project") == "gitlab.corp.com"

    def test_url_with_port(self):
        """URL 带自定义端口"""
        assert extract_instance_key(url="https://gitlab.local:8443/repo") == "gitlab.local:8443"

    def test_url_normalized(self):
        """URL 被规范化"""
        assert extract_instance_key(url="HTTPS://GITLAB.COM:443/") == "gitlab.com"

    # === 空值和边界情况 ===
    
    def test_none_inputs(self):
        """两个参数都是 None 返回 None"""
        assert extract_instance_key(None, None) is None

    def test_empty_payload(self):
        """空 payload 使用 URL"""
        assert extract_instance_key(payload_json={}, url="https://gitlab.io/") == "gitlab.io"

    def test_payload_empty_instance(self):
        """payload 中 gitlab_instance 为空时使用 URL"""
        payload = {"gitlab_instance": ""}
        assert extract_instance_key(payload_json=payload, url="https://gitlab.io/") == "gitlab.io"

    def test_payload_non_string_instance(self):
        """payload 中 gitlab_instance 非字符串时使用 URL"""
        payload = {"gitlab_instance": None}
        assert extract_instance_key(payload_json=payload, url="https://gitlab.io/") == "gitlab.io"


class TestExtractInstanceAndTenant:
    """extract_instance_and_tenant 函数测试"""

    def test_both_extracted(self):
        """同时提取 instance 和 tenant"""
        payload = {"gitlab_instance": "gitlab.example.com", "tenant_id": "acme"}
        result = extract_instance_and_tenant(payload_json=payload)
        assert result == ("gitlab.example.com", "acme")

    def test_from_url_and_project_key(self):
        """从 URL 和 project_key 提取"""
        result = extract_instance_and_tenant(
            url="https://gitlab.corp.com/repo",
            project_key="tenant-x/project"
        )
        assert result == ("gitlab.corp.com", "tenant-x")

    def test_partial_none(self):
        """部分值可能为 None"""
        result = extract_instance_and_tenant(url="https://gitlab.io/")
        assert result == ("gitlab.io", None)

    def test_all_none(self):
        """全部为 None"""
        result = extract_instance_and_tenant()
        assert result == (None, None)


class TestEdgeCasesAndConsistency:
    """边界情况和一致性测试"""

    def test_consistency_case_insensitive(self):
        """大小写不敏感：不同大小写产生相同结果"""
        urls = [
            "https://GITLAB.EXAMPLE.COM/",
            "https://gitlab.example.com/",
            "https://GitLab.Example.Com/",
            "GITLAB.EXAMPLE.COM",
            "gitlab.example.com",
        ]
        results = [normalize_instance_key(u) for u in urls]
        assert all(r == "gitlab.example.com" for r in results)

    def test_consistency_port_normalization(self):
        """端口归一化：默认端口被移除，一致性"""
        variants = [
            "gitlab.example.com",
            "gitlab.example.com:443",
            "https://gitlab.example.com:443/",
            "https://gitlab.example.com/path",
        ]
        results = [normalize_instance_key(v) for v in variants]
        assert all(r == "gitlab.example.com" for r in results)

    def test_consistency_protocol_ignored(self):
        """协议归一化：HTTP/HTTPS 产生相同结果"""
        http_result = normalize_instance_key("http://gitlab.example.com/")
        https_result = normalize_instance_key("https://gitlab.example.com/")
        no_protocol = normalize_instance_key("gitlab.example.com")
        
        assert http_result == https_result == no_protocol == "gitlab.example.com"

    def test_real_world_gitlab_urls(self):
        """真实世界 GitLab URL 测试"""
        test_cases = [
            ("https://gitlab.com/owner/repo", "gitlab.com"),
            ("https://gitlab.example.com/group/subgroup/project.git", "gitlab.example.com"),
            ("http://gitlab.corp.internal:8080/team/project", "gitlab.corp.internal:8080"),
            ("https://code.company.com/", "code.company.com"),
        ]
        for url, expected in test_cases:
            assert normalize_instance_key(url) == expected, f"Failed for {url}"

    def test_real_world_project_keys(self):
        """真实世界 project_key 测试"""
        test_cases = [
            ("company/project", "company"),
            ("org/team/repo", "org"),
            ("single", None),
            ("", None),
        ]
        for project_key, expected in test_cases:
            result = extract_tenant_id(project_key=project_key)
            assert result == expected, f"Failed for {project_key}"

    def test_payload_consistency(self):
        """payload 一致性：scheduler 写入 -> worker/queue 读取"""
        # 模拟 scheduler 写入的 payload
        scheduler_payload = {
            "gitlab_instance": normalize_instance_key("HTTPS://GITLAB.CORP.COM:443/"),
            "tenant_id": extract_tenant_id(project_key="acme/project"),
        }
        
        # 模拟 worker 读取
        instance = extract_instance_key(payload_json=scheduler_payload)
        tenant = extract_tenant_id(payload_json=scheduler_payload)
        
        assert instance == "gitlab.corp.com"
        assert tenant == "acme"


class TestClaimAllowlistNormalization:
    """claim allowlist 规范化测试（验证 queue 模块的一致性）"""

    def test_allowlist_normalization(self):
        """allowlist 值应该被规范化以匹配存储的格式"""
        # 用户可能输入各种格式
        user_inputs = [
            "GITLAB.EXAMPLE.COM",
            "gitlab.example.com:443",
            "https://gitlab.example.com/",
        ]
        
        # 规范化后应该都是相同的
        normalized = [normalize_instance_key(i) for i in user_inputs]
        assert all(n == "gitlab.example.com" for n in normalized)

    def test_empty_allowlist_item(self):
        """空的 allowlist 项会被过滤"""
        items = ["gitlab.example.com", "", "  ", None]
        normalized = [normalize_instance_key(i) for i in items if normalize_instance_key(i)]
        assert normalized == ["gitlab.example.com"]
