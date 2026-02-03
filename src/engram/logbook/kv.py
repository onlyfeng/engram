# -*- coding: utf-8 -*-
"""
kv - logbook.kv 轻量 KV 工具

本模块提供面向“已有 psycopg 连接”的 KV 读写能力（不负责建立连接/提交事务）。

备注：
- 如果你希望使用 DSN/config 驱动的 KV 操作，请使用 `engram.logbook.db.set_kv/get_kv`。
"""

from __future__ import annotations

import json
from typing import Any, Optional, cast

import psycopg


def kv_set_json(
    conn: psycopg.Connection[Any],
    namespace: str,
    key: str,
    value: dict[str, Any],
) -> None:
    """写入 JSON 到 logbook.kv（存在则更新）。"""
    if not namespace:
        raise ValueError("namespace 不能为空")
    if not key:
        raise ValueError("key 不能为空")

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


def kv_get_json(
    conn: psycopg.Connection[Any],
    namespace: str,
    key: str,
) -> Optional[dict[str, Any]]:
    """从 logbook.kv 读取 JSON 值，不存在返回 None。"""
    if not namespace:
        raise ValueError("namespace 不能为空")
    if not key:
        raise ValueError("key 不能为空")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT value_json
            FROM logbook.kv
            WHERE namespace = %s AND key = %s
            """,
            (namespace, key),
        )
        row = cur.fetchone()
        if not row:
            return None

        value_json = row[0]
        if value_json is None:
            return None
        if isinstance(value_json, dict):
            return cast(dict[str, Any], value_json)
        if isinstance(value_json, str):
            parsed = json.loads(value_json)
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            return None
        # psycopg 通常会返回 dict，但为兼容性保守处理
        try:
            return cast(dict[str, Any], dict(value_json))
        except Exception:
            return None


__all__ = [
    "kv_set_json",
    "kv_get_json",
]
