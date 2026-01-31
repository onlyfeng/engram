#!/usr/bin/env python3
"""
engram_logbook.backfill_chunking_version - 回填 chunking_version 字段
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

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 1000


def get_patch_blobs_to_update(
    conn: psycopg.Connection,
    target_version: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    only_missing: bool = False,
) -> List[Dict[str, Any]]:
    if only_missing:
        query = """
            SELECT blob_id, source_type, source_id, sha256, chunking_version
            FROM scm.patch_blobs
            WHERE chunking_version IS NULL
            ORDER BY blob_id
            LIMIT %s
        """
        params = (batch_size,)
    else:
        query = """
            SELECT blob_id, source_type, source_id, sha256, chunking_version
            FROM scm.patch_blobs
            WHERE chunking_version IS NULL OR chunking_version != %s
            ORDER BY blob_id
            LIMIT %s
        """
        params = (target_version, batch_size)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def get_attachments_to_update(
    conn: psycopg.Connection,
    target_version: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    only_missing: bool = False,
) -> List[Dict[str, Any]]:
    if only_missing:
        query = """
            SELECT attachment_id, item_id, kind, uri, sha256, meta_json
            FROM logbook.attachments
            WHERE meta_json->>'chunking_version' IS NULL
            ORDER BY attachment_id
            LIMIT %s
        """
        params = (batch_size,)
    else:
        query = """
            SELECT attachment_id, item_id, kind, uri, sha256, meta_json
            FROM logbook.attachments
            WHERE meta_json->>'chunking_version' IS NULL
               OR meta_json->>'chunking_version' != %s
            ORDER BY attachment_id
            LIMIT %s
        """
        params = (target_version, batch_size)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def update_patch_blob_chunking_version(
    conn: psycopg.Connection, blob_id: int, chunking_version: str
) -> bool:
    query = """
        UPDATE scm.patch_blobs
        SET chunking_version = %s,
            updated_at = now()
        WHERE blob_id = %s
        RETURNING blob_id
    """
    with conn.cursor() as cur:
        cur.execute(query, (chunking_version, blob_id))
        return cur.fetchone() is not None


def update_attachment_chunking_version(
    conn: psycopg.Connection, attachment_id: int, chunking_version: str
) -> bool:
    query = """
        UPDATE logbook.attachments
        SET meta_json = COALESCE(meta_json, '{}'::jsonb) || %s::jsonb
        WHERE attachment_id = %s
        RETURNING attachment_id
    """
    with conn.cursor() as cur:
        cur.execute(query, (json.dumps({"chunking_version": chunking_version}), attachment_id))
        return cur.fetchone() is not None


def backfill_patch_blobs(
    conn: psycopg.Connection,
    target_version: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    only_missing: bool = False,
) -> Dict[str, Any]:
    total_processed = 0
    total_updated = 0
    total_failed = 0

    while True:
        blobs = get_patch_blobs_to_update(conn, target_version, batch_size, only_missing)
        if not blobs:
            break

        for blob in blobs:
            blob_id = blob["blob_id"]
            if dry_run:
                total_updated += 1
            else:
                try:
                    if update_patch_blob_chunking_version(conn, blob_id, target_version):
                        total_updated += 1
                    else:
                        total_failed += 1
                except Exception as e:
                    logger.error("更新 patch_blobs blob_id=%s 出错: %s", blob_id, e)
                    total_failed += 1
            total_processed += 1

        if not dry_run:
            conn.commit()

        if len(blobs) < batch_size:
            break

    return {
        "total_processed": total_processed,
        "total_updated": total_updated,
        "total_failed": total_failed,
    }


def backfill_attachments(
    conn: psycopg.Connection,
    target_version: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    only_missing: bool = False,
) -> Dict[str, Any]:
    total_processed = 0
    total_updated = 0
    total_failed = 0

    while True:
        attachments = get_attachments_to_update(conn, target_version, batch_size, only_missing)
        if not attachments:
            break

        for attachment in attachments:
            attachment_id = attachment["attachment_id"]
            if dry_run:
                total_updated += 1
            else:
                try:
                    if update_attachment_chunking_version(conn, attachment_id, target_version):
                        total_updated += 1
                    else:
                        total_failed += 1
                except Exception as e:
                    logger.error("更新 attachments attachment_id=%s 出错: %s", attachment_id, e)
                    total_failed += 1
            total_processed += 1

        if not dry_run:
            conn.commit()

        if len(attachments) < batch_size:
            break

    return {
        "total_processed": total_processed,
        "total_updated": total_updated,
        "total_failed": total_failed,
    }


def backfill_chunking_version(
    target_version: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    only_missing: bool = False,
    config: Optional[Config] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "target_version": target_version,
        "dry_run": dry_run,
        "only_missing": only_missing,
        "patch_blobs": {"total_processed": 0, "total_updated": 0, "total_failed": 0},
        "attachments": {"total_processed": 0, "total_updated": 0, "total_failed": 0},
    }

    conn = get_connection(config=config)
    try:
        patch_blobs_result = backfill_patch_blobs(
            conn, target_version, batch_size=batch_size, dry_run=dry_run, only_missing=only_missing
        )
        attachments_result = backfill_attachments(
            conn, target_version, batch_size=batch_size, dry_run=dry_run, only_missing=only_missing
        )

        result["patch_blobs"] = patch_blobs_result
        result["attachments"] = attachments_result
        result["success"] = True

        result["summary"] = {
            "total_processed": patch_blobs_result["total_processed"]
            + attachments_result["total_processed"],
            "total_updated": patch_blobs_result["total_updated"]
            + attachments_result["total_updated"],
            "total_failed": patch_blobs_result["total_failed"] + attachments_result["total_failed"],
        }
    except Exception as e:
        result["error"] = str(e)
    finally:
        conn.close()

    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填 chunking_version 字段")
    add_config_argument(parser)
    parser.add_argument("--chunking-version", required=True, dest="chunking_version")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-missing", action="store_true")
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
    res = backfill_chunking_version(
        target_version=args.chunking_version,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        only_missing=args.only_missing,
        config=config,
    )
    if args.json:
        print(json.dumps(res, default=str, ensure_ascii=False))
    return 0 if res.get("success") else 1


__all__ = ["backfill_chunking_version", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
