# -*- coding: utf-8 -*-
"""
engram_logbook.scm_sync_keys - SCM 同步键名规范化模块

提供统一的实例标识、租户 ID 提取和规范化函数。

功能:
- normalize_instance_key: 将 URL 或 host 规范化为唯一实例标识
- extract_tenant_id: 从 payload_json 或 project_key 提取租户 ID
- extract_instance_key: 从 payload_json 或 URL 提取实例标识

设计原则:
- 大小写不敏感（统一转小写）
- 端口归一化（忽略默认端口 80/443）
- 协议归一化（忽略协议差异，只保留 host:port）
- 空值安全（返回 None 而非抛出异常）
"""

from typing import Any, Dict, Optional
from urllib.parse import urlparse


def normalize_instance_key(url_or_host: Optional[str]) -> Optional[str]:
    """
    将 URL 或 host 规范化为唯一实例标识。

    规范化规则:
    - 统一转换为小写
    - 提取 host 部分（忽略路径）
    - 移除默认端口（:80 或 :443）
    - 移除协议前缀（http:// 或 https://）

    示例:
        normalize_instance_key("https://GitLab.Example.COM/group/proj")
        -> "gitlab.example.com"

        normalize_instance_key("GITLAB.CORP.COM:443")
        -> "gitlab.corp.com"

        normalize_instance_key("http://gitlab.local:8080/")
        -> "gitlab.local:8080"

        normalize_instance_key("gitlab.io")
        -> "gitlab.io"

    Args:
        url_or_host: URL 字符串或 host 字符串

    Returns:
        规范化后的实例标识（小写），或 None（输入为空时）
    """
    if not url_or_host:
        return None

    value = url_or_host.strip()
    if not value:
        return None

    # 检查是否包含协议
    if "://" in value:
        try:
            parsed = urlparse(value)
            host = parsed.netloc or parsed.hostname or ""
        except Exception:
            # 解析失败，尝试简单处理
            host = value.split("://", 1)[-1].split("/", 1)[0]
    else:
        # 没有协议，可能是 host 或 host:port
        host = value.split("/", 1)[0]

    # 转换为小写
    host = host.lower()

    # 移除默认端口
    if host.endswith(":80"):
        host = host[:-3]
    elif host.endswith(":443"):
        host = host[:-4]

    return host if host else None


def extract_tenant_id(
    payload_json: Optional[Dict[str, Any]] = None,
    project_key: Optional[str] = None,
) -> Optional[str]:
    """
    从 payload_json 或 project_key 提取租户 ID。

    提取优先级:
    1. payload_json 中的 "tenant_id" 字段
    2. project_key 中 "/" 前的部分

    示例:
        extract_tenant_id({"tenant_id": "acme"})
        -> "acme"

        extract_tenant_id(project_key="tenant-a/project-x")
        -> "tenant-a"

        extract_tenant_id(project_key="single_project")
        -> None

        extract_tenant_id({}, "")
        -> None

    Args:
        payload_json: 任务 payload 字典
        project_key: 项目键名

    Returns:
        租户 ID 字符串，或 None（未找到时）
    """
    # 优先从 payload_json 获取
    if payload_json and isinstance(payload_json, dict):
        tenant_id = payload_json.get("tenant_id")
        if tenant_id and isinstance(tenant_id, str) and tenant_id.strip():
            result: str = tenant_id.strip()
            return result

    # 从 project_key 解析
    if project_key and isinstance(project_key, str):
        project_key = project_key.strip()
        if "/" in project_key:
            tenant_part = project_key.split("/", 1)[0].strip()
            if tenant_part:
                return tenant_part

    return None


def extract_instance_key(
    payload_json: Optional[Dict[str, Any]] = None,
    url: Optional[str] = None,
) -> Optional[str]:
    """
    从 payload_json 或 URL 提取规范化的实例标识。

    提取优先级:
    1. payload_json 中的 "gitlab_instance" 字段（会做规范化）
    2. 从 URL 解析并规范化

    示例:
        extract_instance_key({"gitlab_instance": "GitLab.Example.COM"})
        -> "gitlab.example.com"

        extract_instance_key(url="https://gitlab.corp.com:443/group/proj")
        -> "gitlab.corp.com"

        extract_instance_key({}, "")
        -> None

    Args:
        payload_json: 任务 payload 字典
        url: 仓库 URL

    Returns:
        规范化后的实例标识（小写），或 None（未找到时）
    """
    # 优先从 payload_json 获取
    if payload_json and isinstance(payload_json, dict):
        gitlab_instance = payload_json.get("gitlab_instance")
        if gitlab_instance and isinstance(gitlab_instance, str):
            normalized = normalize_instance_key(gitlab_instance)
            if normalized:
                return normalized

    # 从 URL 解析
    if url and isinstance(url, str):
        return normalize_instance_key(url)

    return None


def extract_instance_and_tenant(
    payload_json: Optional[Dict[str, Any]] = None,
    url: Optional[str] = None,
    project_key: Optional[str] = None,
) -> tuple:
    """
    同时提取实例标识和租户 ID。

    便捷函数，一次调用获取两个值。

    Args:
        payload_json: 任务 payload 字典
        url: 仓库 URL
        project_key: 项目键名

    Returns:
        (instance_key, tenant_id) 元组，任一值可能为 None
    """
    instance_key = extract_instance_key(payload_json=payload_json, url=url)
    tenant_id = extract_tenant_id(payload_json=payload_json, project_key=project_key)
    return (instance_key, tenant_id)
