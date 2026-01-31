"""
hash_utils - 哈希计算纯函数模块

提供 payload SHA256 计算等哈希相关的纯函数。
"""

import hashlib


def compute_payload_sha(payload_md: str) -> str:
    """
    计算 payload 的 SHA256 哈希

    Args:
        payload_md: Markdown 格式的 payload 内容

    Returns:
        64 字符的十六进制 SHA256 哈希值
    """
    return hashlib.sha256(payload_md.encode("utf-8")).hexdigest()
