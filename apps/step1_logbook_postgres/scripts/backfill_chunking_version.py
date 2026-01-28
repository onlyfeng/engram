#!/usr/bin/env python3
"""
backfill_chunking_version.py - 回填 chunking_version 字段

功能:
- 扫描 scm.patch_blobs 表中 chunking_version 为 NULL 或与目标版本不一致的记录
- 扫描 logbook.attachments 表中 meta_json 里 chunking_version 缺失或不一致的记录
- 更新 chunking_version 字段
- 输出 JSON 统计，供自动化流水线消费

使用:
    python backfill_chunking_version.py --chunking-version v1.0 [--dry-run] [--batch-size N] [--only-missing]

筛选逻辑:
    patch_blobs:   chunking_version IS NULL OR chunking_version != <ver>
    attachments:   meta_json->>'chunking_version' IS NULL OR meta_json->>'chunking_version' != <ver>
    --only-missing: 仅处理 IS NULL 的记录
"""

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row

from engram_step1.config import Config, add_config_argument, get_config
from engram_step1.db import get_connection

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 默认配置值
DEFAULT_BATCH_SIZE = 1000


def get_patch_blobs_to_update(
    conn: psycopg.Connection,
    target_version: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    only_missing: bool = False,
) -> List[Dict[str, Any]]:
    """
    获取需要更新 chunking_version 的 patch_blobs 记录
    
    Args:
        conn: 数据库连接
        target_version: 目标 chunking_version
        batch_size: 批量大小
        only_missing: 是否仅处理 chunking_version 为 NULL 的记录
    
    Returns:
        记录列表
    """
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
    """
    获取需要更新 chunking_version 的 attachments 记录
    
    Args:
        conn: 数据库连接
        target_version: 目标 chunking_version
        batch_size: 批量大小
        only_missing: 是否仅处理 meta_json 中 chunking_version 为 NULL 的记录
    
    Returns:
        记录列表
    """
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
    conn: psycopg.Connection,
    blob_id: int,
    chunking_version: str,
) -> bool:
    """
    更新 patch_blobs 记录的 chunking_version
    
    Args:
        conn: 数据库连接
        blob_id: blob ID
        chunking_version: 目标 chunking_version
    
    Returns:
        True 如果更新成功
    """
    query = """
        UPDATE scm.patch_blobs
        SET chunking_version = %s,
            updated_at = now()
        WHERE blob_id = %s
        RETURNING blob_id
    """
    
    with conn.cursor() as cur:
        cur.execute(query, (chunking_version, blob_id))
        result = cur.fetchone()
        return result is not None


def update_attachment_chunking_version(
    conn: psycopg.Connection,
    attachment_id: int,
    chunking_version: str,
) -> bool:
    """
    更新 attachments 记录的 meta_json 中的 chunking_version
    
    Args:
        conn: 数据库连接
        attachment_id: attachment ID
        chunking_version: 目标 chunking_version
    
    Returns:
        True 如果更新成功
    """
    query = """
        UPDATE logbook.attachments
        SET meta_json = COALESCE(meta_json, '{}'::jsonb) || %s::jsonb
        WHERE attachment_id = %s
        RETURNING attachment_id
    """
    
    with conn.cursor() as cur:
        cur.execute(query, (json.dumps({"chunking_version": chunking_version}), attachment_id))
        result = cur.fetchone()
        return result is not None


def backfill_patch_blobs(
    conn: psycopg.Connection,
    target_version: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    only_missing: bool = False,
) -> Dict[str, Any]:
    """
    批量回填 patch_blobs 的 chunking_version
    
    Args:
        conn: 数据库连接
        target_version: 目标 chunking_version
        batch_size: 批量大小
        dry_run: 是否仅模拟执行
        only_missing: 是否仅处理 chunking_version 为 NULL 的记录
    
    Returns:
        处理结果统计
    """
    total_processed = 0
    total_updated = 0
    total_failed = 0
    
    while True:
        blobs = get_patch_blobs_to_update(conn, target_version, batch_size, only_missing)
        
        if not blobs:
            logger.info("patch_blobs: 无更多记录需要处理")
            break
        
        logger.info(f"patch_blobs: 本批次处理 {len(blobs)} 条记录")
        
        for blob in blobs:
            blob_id = blob["blob_id"]
            
            if dry_run:
                logger.info(f"[DRY-RUN] patch_blobs blob_id={blob_id}: {blob.get('chunking_version')} -> {target_version}")
                total_updated += 1
            else:
                try:
                    success = update_patch_blob_chunking_version(conn, blob_id, target_version)
                    if success:
                        total_updated += 1
                        logger.debug(f"更新 patch_blobs blob_id={blob_id}: {target_version}")
                    else:
                        total_failed += 1
                        logger.warning(f"更新 patch_blobs blob_id={blob_id} 失败")
                except Exception as e:
                    total_failed += 1
                    logger.error(f"更新 patch_blobs blob_id={blob_id} 出错: {e}")
            
            total_processed += 1
        
        # 提交本批次
        if not dry_run:
            conn.commit()
        
        logger.info(
            f"patch_blobs 累计: 处理={total_processed}, 更新={total_updated}, 失败={total_failed}"
        )
        
        # 如果获取到的记录数少于 batch_size，说明已处理完毕
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
    """
    批量回填 attachments 的 chunking_version
    
    Args:
        conn: 数据库连接
        target_version: 目标 chunking_version
        batch_size: 批量大小
        dry_run: 是否仅模拟执行
        only_missing: 是否仅处理 meta_json 中 chunking_version 为 NULL 的记录
    
    Returns:
        处理结果统计
    """
    total_processed = 0
    total_updated = 0
    total_failed = 0
    
    while True:
        attachments = get_attachments_to_update(conn, target_version, batch_size, only_missing)
        
        if not attachments:
            logger.info("attachments: 无更多记录需要处理")
            break
        
        logger.info(f"attachments: 本批次处理 {len(attachments)} 条记录")
        
        for attachment in attachments:
            attachment_id = attachment["attachment_id"]
            meta = attachment.get("meta_json") or {}
            current_version = meta.get("chunking_version") if isinstance(meta, dict) else None
            
            if dry_run:
                logger.info(
                    f"[DRY-RUN] attachments attachment_id={attachment_id}: "
                    f"{current_version} -> {target_version}"
                )
                total_updated += 1
            else:
                try:
                    success = update_attachment_chunking_version(conn, attachment_id, target_version)
                    if success:
                        total_updated += 1
                        logger.debug(f"更新 attachments attachment_id={attachment_id}: {target_version}")
                    else:
                        total_failed += 1
                        logger.warning(f"更新 attachments attachment_id={attachment_id} 失败")
                except Exception as e:
                    total_failed += 1
                    logger.error(f"更新 attachments attachment_id={attachment_id} 出错: {e}")
            
            total_processed += 1
        
        # 提交本批次
        if not dry_run:
            conn.commit()
        
        logger.info(
            f"attachments 累计: 处理={total_processed}, 更新={total_updated}, 失败={total_failed}"
        )
        
        # 如果获取到的记录数少于 batch_size，说明已处理完毕
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
    """
    批量回填 chunking_version
    
    Args:
        target_version: 目标 chunking_version
        batch_size: 批量大小
        dry_run: 是否仅模拟执行
        only_missing: 是否仅处理缺失的记录
        config: 配置实例
    
    Returns:
        处理结果统计
    """
    result = {
        "success": False,
        "target_version": target_version,
        "dry_run": dry_run,
        "only_missing": only_missing,
        "patch_blobs": {
            "total_processed": 0,
            "total_updated": 0,
            "total_failed": 0,
        },
        "attachments": {
            "total_processed": 0,
            "total_updated": 0,
            "total_failed": 0,
        },
    }
    
    conn = get_connection(config=config)
    try:
        # 处理 patch_blobs
        logger.info("=== 开始处理 scm.patch_blobs ===")
        patch_blobs_result = backfill_patch_blobs(
            conn, target_version, batch_size, dry_run, only_missing
        )
        result["patch_blobs"] = patch_blobs_result
        
        # 处理 attachments
        logger.info("=== 开始处理 logbook.attachments ===")
        attachments_result = backfill_attachments(
            conn, target_version, batch_size, dry_run, only_missing
        )
        result["attachments"] = attachments_result
        
        result["success"] = True
        
        # 汇总统计
        total_processed = (
            patch_blobs_result["total_processed"] + attachments_result["total_processed"]
        )
        total_updated = (
            patch_blobs_result["total_updated"] + attachments_result["total_updated"]
        )
        total_failed = (
            patch_blobs_result["total_failed"] + attachments_result["total_failed"]
        )
        
        result["summary"] = {
            "total_processed": total_processed,
            "total_updated": total_updated,
            "total_failed": total_failed,
        }
        
        logger.info(
            f"回填完成: 处理={total_processed}, 更新={total_updated}, 失败={total_failed}"
        )
        
    except Exception as e:
        logger.exception(f"回填过程中发生错误: {e}")
        result["error"] = str(e)
    finally:
        conn.close()
    
    return result


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="回填 chunking_version 字段",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 实际执行回填（更新所有不匹配的记录）
    python backfill_chunking_version.py --chunking-version v1.0

    # 模拟执行（不实际修改数据）
    python backfill_chunking_version.py --chunking-version v1.0 --dry-run

    # 仅处理 chunking_version 为 NULL 的记录
    python backfill_chunking_version.py --chunking-version v1.0 --only-missing

    # 指定批量大小
    python backfill_chunking_version.py --chunking-version v1.0 --batch-size 500

    # JSON 输出（供自动化流水线消费）
    python backfill_chunking_version.py --chunking-version v1.0 --json
        """,
    )
    
    add_config_argument(parser)
    
    parser.add_argument(
        "--chunking-version",
        type=str,
        required=True,
        help="目标 chunking_version 版本号（必需）",
    )
    
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"每批处理的记录数 (默认: {DEFAULT_BATCH_SIZE})",
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模拟执行，不实际修改数据",
    )
    
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="仅处理 chunking_version 为 NULL 的记录（跳过版本不匹配的记录）",
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )
    
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果（供自动化流水线消费）",
    )
    
    return parser.parse_args()


def main() -> int:
    """主入口"""
    args = parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        # 加载配置
        config = get_config(args.config_path)
        config.load()
        
        if args.dry_run:
            logger.info("=== DRY-RUN 模式：不会实际修改数据 ===")
        
        if args.only_missing:
            logger.info("=== ONLY-MISSING 模式：仅处理 chunking_version 为 NULL 的记录 ===")
        
        logger.info(f"目标版本: {args.chunking_version}")
        
        # 执行回填
        result = backfill_chunking_version(
            target_version=args.chunking_version,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            only_missing=args.only_missing,
            config=config,
        )
        
        if args.json:
            print(json.dumps(result, default=str, ensure_ascii=False))
        else:
            summary = result.get("summary", {})
            print(
                f"回填完成: 处理={summary.get('total_processed', 0)}, "
                f"更新={summary.get('total_updated', 0)}, "
                f"失败={summary.get('total_failed', 0)}"
            )
            print(f"  patch_blobs:  处理={result['patch_blobs']['total_processed']}, "
                  f"更新={result['patch_blobs']['total_updated']}, "
                  f"失败={result['patch_blobs']['total_failed']}")
            print(f"  attachments:  处理={result['attachments']['total_processed']}, "
                  f"更新={result['attachments']['total_updated']}, "
                  f"失败={result['attachments']['total_failed']}")
        
        return 0 if result["success"] else 1
        
    except Exception as e:
        logger.exception(f"未预期的错误: {e}")
        if args.json:
            print(json.dumps({
                "success": False,
                "error": True,
                "type": "UNEXPECTED_ERROR",
                "message": str(e),
            }, default=str, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
