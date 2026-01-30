#!/usr/bin/env python3
"""
kv - 简易 KV 写入工具（测试用）
"""

from __future__ import annotations

import json
from typing import Any, Dict


def kv_set_json(conn, namespace: str, key: str, value: Dict[str, Any]) -> None:
    """写入 JSON 到 logbook.kv（存在则更新）"""
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
