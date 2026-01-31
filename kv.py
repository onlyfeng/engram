#!/usr/bin/env python3
"""
kv - 简易 KV 写入工具兼容包装器

[DEPRECATED] 此模块已废弃，请使用 engram.logbook.scm_db 中的 KV 相关函数代替。

此模块保留用于向后兼容和验收测试验证 deprecation 转发仍可用。
新代码应直接使用:

    from engram.logbook.scm_db import save_circuit_breaker_state, load_circuit_breaker_state, ...

此包装器将在未来版本中移除。
"""

from __future__ import annotations

import json
import warnings
from typing import Any, Dict

# 发出废弃警告
warnings.warn(
    "kv.py 已废弃。请使用 'from engram.logbook.scm_db import ...' 中的 KV 相关函数代替。"
    "此模块将在未来版本中移除。",
    DeprecationWarning,
    stacklevel=2,
)


def kv_set_json(conn, namespace: str, key: str, value: Dict[str, Any]) -> None:
    """
    写入 JSON 到 logbook.kv（存在则更新）

    [DEPRECATED] 此函数已废弃，新代码请使用 engram.logbook.scm_db 中的相关函数。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO logbook.kv (namespace, key, value_json)
            VALUES (%s, %s, %s)
            ON CONFLICT (namespace, key) DO UPDATE
            SET value_json = EXCLUDED.value_json,
                updated_at = now()
            """,
            (namespace, key, json.dumps(value)),
        )
