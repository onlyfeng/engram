#!/usr/bin/env python3
"""
engram_logbook.backfill_evidence_uri - 回填 patch_blobs 的 evidence_uri
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row

from .config import Config, add_config_argument, get_config
from .db import get_connection
from .uri import build_evidence_uri

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 1000


def get_blobs_missing_evidence_uri(
    conn: psycopg.Connection,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> List[Dict[str, Any]]:
    query = """
        SELECT blob_id, source_type, source_id, sha256, meta_json
        FROM scm.patch_blobs
        WHERE meta_json IS NULL
           OR meta_json->>'evidence_uri' IS NULL
           OR meta_json->>'evidence_uri' = ''
        ORDER BY blob_id
        LIMIT %s
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, (batch_size,))
        return cur.fetchall()


def update_evidence_uri(conn: psycopg.Connection, blob_id: int, evidence_uri: str) -> bool:
    query = """
        UPDATE scm.patch_blobs
        SET meta_json = COALESCE(meta_json, '{}'::jsonb) || %s::jsonb,
            updated_at = now()
        WHERE blob_id = %s
        RETURNING blob_id
    """
    with conn.cursor() as cur:
        cur.execute(query, (json.dumps({"evidence_uri": evidence_uri}), blob_id))
        return cur.fetchone() is not None


def backfill_evidence_uri(
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    config: Optional[Config] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "total_processed": 0,
        "total_updated": 0,
        "total_skipped": 0,
        "total_failed": 0,
        "dry_run": dry_run,
    }

    conn = get_connection(config=config)
    try:
        total_processed = 0
        total_updated = 0
        total_skipped = 0
        total_failed = 0

        while True:
            blobs = get_blobs_missing_evidence_uri(conn, batch_size)
            if not blobs:
                break

            for blob in blobs:
                blob_id = blob["blob_id"]
                source_type = blob["source_type"]
                source_id = blob["source_id"]
                sha256 = blob["sha256"]

                if not source_type or not source_id or not sha256:
                    total_skipped += 1
                    continue

                evidence_uri = build_evidence_uri(source_type, source_id, sha256)

                if dry_run:
                    total_updated += 1
                else:
                    try:
                        if update_evidence_uri(conn, blob_id, evidence_uri):
                            total_updated += 1
                        else:
                            total_failed += 1
                    except Exception as e:
                        logger.error("更新 blob_id=%s 出错: %s", blob_id, e)
                        total_failed += 1

                total_processed += 1

            if not dry_run:
                conn.commit()

            if len(blobs) < batch_size:
                break

        result.update(
            {
                "success": True,
                "total_processed": total_processed,
                "total_updated": total_updated,
                "total_skipped": total_skipped,
                "total_failed": total_failed,
            }
        )
    except Exception as e:
        result["error"] = str(e)
    finally:
        conn.close()

    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填 patch_blobs 的 evidence_uri")
    add_config_argument(parser)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = get_config(args.config_path)
    config.load()

    res = backfill_evidence_uri(batch_size=args.batch_size, dry_run=args.dry_run, config=config)
    if args.json:
        print(json.dumps(res, default=str, ensure_ascii=False))
    return 0 if res.get("success") else 1


__all__ = ["backfill_evidence_uri", "main"]

