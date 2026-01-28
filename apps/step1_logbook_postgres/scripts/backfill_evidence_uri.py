#!/usr/bin/env python3
"""
backfill_evidence_uri.py - 回填 patch_blobs 的 evidence_uri

功能:
- 扫描 scm.patch_blobs 表中缺失 evidence_uri 的记录
- 根据 source_type、source_id、sha256 生成 canonical evidence_uri
- 双写更新：evidence_uri 列 + meta_json->>'evidence_uri'（保持向后兼容）

使用:
    python backfill_evidence_uri.py [--config PATH] [--batch-size N] [--dry-run] [--verbose]

Canonical Evidence URI 格式:
    memory://patch_blobs/<source_type>/<source_id>/<sha256>

兼容性说明:
- 查询时使用 COALESCE(evidence_uri, meta_json->>'evidence_uri') 兼容新旧数据
- 写入时双写（列 + meta_json）确保向后兼容
- 迁移后优先使用 evidence_uri 列（性能更优）
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
from engram_step1.uri import build_evidence_uri

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 默认配置值
DEFAULT_BATCH_SIZE = 1000


def get_blobs_missing_evidence_uri(
    conn: psycopg.Connection,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> List[Dict[str, Any]]:
    """
    获取缺失 evidence_uri 的 patch_blobs 记录
    
    检查条件（使用 COALESCE 兼容新旧数据）：
    - evidence_uri 列为空 且
    - meta_json->>'evidence_uri' 为空
    
    Args:
        conn: 数据库连接
        batch_size: 批量大小
    
    Returns:
        记录列表
    """
    query = """
        SELECT blob_id, source_type, source_id, sha256, meta_json, evidence_uri
        FROM scm.patch_blobs
        WHERE COALESCE(evidence_uri, meta_json->>'evidence_uri', '') = ''
        ORDER BY blob_id
        LIMIT %s
    """
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, (batch_size,))
        return cur.fetchall()


def update_evidence_uri(
    conn: psycopg.Connection,
    blob_id: int,
    evidence_uri: str,
) -> bool:
    """
    更新单条记录的 evidence_uri（双写：列 + meta_json）
    
    同时更新:
    - evidence_uri 列（新增的独立列）
    - meta_json->>'evidence_uri'（保持向后兼容）
    
    Args:
        conn: 数据库连接
        blob_id: blob ID
        evidence_uri: canonical evidence URI
    
    Returns:
        True 如果更新成功
    """
    query = """
        UPDATE scm.patch_blobs
        SET evidence_uri = %s,
            meta_json = COALESCE(meta_json, '{}'::jsonb) || %s::jsonb,
            updated_at = now()
        WHERE blob_id = %s
        RETURNING blob_id
    """
    
    with conn.cursor() as cur:
        cur.execute(query, (evidence_uri, json.dumps({"evidence_uri": evidence_uri}), blob_id))
        result = cur.fetchone()
        return result is not None


def backfill_evidence_uri(
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    config: Optional[Config] = None,
) -> Dict[str, Any]:
    """
    批量回填 evidence_uri
    
    Args:
        batch_size: 批量大小
        dry_run: 是否仅模拟执行
        config: 配置实例
    
    Returns:
        处理结果统计
    """
    result = {
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
            # 获取一批缺失 evidence_uri 的记录
            blobs = get_blobs_missing_evidence_uri(conn, batch_size)
            
            if not blobs:
                logger.info("无更多记录需要处理")
                break
            
            logger.info(f"本批次处理 {len(blobs)} 条记录")
            
            for blob in blobs:
                blob_id = blob["blob_id"]
                source_type = blob["source_type"]
                source_id = blob["source_id"]
                sha256 = blob["sha256"]
                
                # 验证必要字段
                if not source_type or not source_id or not sha256:
                    logger.warning(
                        f"blob_id={blob_id} 缺少必要字段，跳过 "
                        f"(source_type={source_type}, source_id={source_id}, sha256={sha256})"
                    )
                    total_skipped += 1
                    continue
                
                # 生成 canonical evidence_uri
                evidence_uri = build_evidence_uri(source_type, source_id, sha256)
                
                if dry_run:
                    logger.info(f"[DRY-RUN] blob_id={blob_id}: {evidence_uri}")
                    total_updated += 1
                else:
                    try:
                        success = update_evidence_uri(conn, blob_id, evidence_uri)
                        if success:
                            total_updated += 1
                            logger.debug(f"更新 blob_id={blob_id}: {evidence_uri}")
                        else:
                            total_failed += 1
                            logger.warning(f"更新 blob_id={blob_id} 失败")
                    except Exception as e:
                        total_failed += 1
                        logger.error(f"更新 blob_id={blob_id} 出错: {e}")
                
                total_processed += 1
            
            # 提交本批次
            if not dry_run:
                conn.commit()
            
            logger.info(
                f"累计处理: {total_processed}, 更新: {total_updated}, "
                f"跳过: {total_skipped}, 失败: {total_failed}"
            )
            
            # 如果获取到的记录数少于 batch_size，说明已处理完毕
            if len(blobs) < batch_size:
                break
        
        result["success"] = True
        result["total_processed"] = total_processed
        result["total_updated"] = total_updated
        result["total_skipped"] = total_skipped
        result["total_failed"] = total_failed
        
        logger.info(
            f"回填完成: 处理={total_processed}, 更新={total_updated}, "
            f"跳过={total_skipped}, 失败={total_failed}"
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
        description="回填 patch_blobs 的 evidence_uri",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 实际执行回填
    python backfill_evidence_uri.py

    # 模拟执行（不实际修改数据）
    python backfill_evidence_uri.py --dry-run

    # 指定批量大小
    python backfill_evidence_uri.py --batch-size 500
        """,
    )
    
    add_config_argument(parser)
    
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
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )
    
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
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
        
        # 执行回填
        result = backfill_evidence_uri(
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            config=config,
        )
        
        if args.json:
            print(json.dumps(result, default=str, ensure_ascii=False))
        else:
            print(
                f"回填完成: 处理={result['total_processed']}, "
                f"更新={result['total_updated']}, "
                f"跳过={result['total_skipped']}, "
                f"失败={result['total_failed']}"
            )
        
        return 0 if result["success"] else 1
        
    except Exception as e:
        logger.exception(f"未预期的错误: {e}")
        if args.json:
            print(json.dumps({
                "error": True,
                "type": "UNEXPECTED_ERROR",
                "message": str(e),
            }, default=str, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
