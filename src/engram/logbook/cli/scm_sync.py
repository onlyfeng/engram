#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engram.logbook.cli.scm_sync - SCM Sync 子系统统一 CLI 入口

此模块是 SCM Sync 子系统的正式 CLI 入口，整合了：
- scheduler: 调度器 - 扫描仓库并入队同步任务
- worker: Worker - 从队列获取并执行同步任务
- reaper: 清理器 - 回收过期任务、runs 和锁
- status: 状态查询 - 查看同步健康状态与指标
- runner: 同步运行器 - 增量同步与回填工具

使用方式:
    # 统一入口
    python -m engram.logbook.cli.scm_sync scheduler --once
    python -m engram.logbook.cli.scm_sync worker --worker-id worker-1
    python -m engram.logbook.cli.scm_sync reaper --dry-run
    python -m engram.logbook.cli.scm_sync status --json
    python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123

    # 安装后使用 console_scripts
    engram-scm-sync scheduler --once
    engram-scm-sync worker --worker-id worker-1
    engram-scm-sync reaper --dry-run
    engram-scm-sync status --json

    # 子命令快捷入口
    engram-scm-scheduler --once
    engram-scm-worker --worker-id worker-1
    engram-scm-reaper --dry-run
    engram-scm-status --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import List, Optional

# ============ 共享工具函数 ============


def _get_dsn_from_env() -> Optional[str]:
    """从环境变量获取 DSN"""
    return os.environ.get("LOGBOOK_DSN") or os.environ.get("POSTGRES_DSN")


def _get_connection(dsn: Optional[str]):
    """获取数据库连接"""
    if not dsn:
        raise ValueError("未提供数据库连接字符串。请设置 LOGBOOK_DSN 环境变量或使用 --dsn 参数")
    from engram.logbook import scm_db

    return scm_db.get_conn(dsn)


def _setup_logging(verbose: bool = False) -> None:
    """配置日志"""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ============ Scheduler CLI ============

# 默认循环间隔（秒）
DEFAULT_SCHEDULER_INTERVAL_SECONDS = 60
DEFAULT_REAPER_INTERVAL_SECONDS = 60


def scheduler_main(argv: Optional[List[str]] = None) -> int:
    """Scheduler CLI 入口函数"""
    from engram.logbook.scm_sync_policy import (
        CircuitBreakerConfig,
        SchedulerConfig,
    )
    from engram.logbook.scm_sync_scheduler_core import (
        run_scheduler_tick,
    )

    parser = argparse.ArgumentParser(
        description="SCM 同步调度器 - 扫描仓库并入队同步任务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 执行一次调度
    python -m engram.logbook.cli.scm_sync scheduler --once

    # 循环模式运行
    python -m engram.logbook.cli.scm_sync scheduler --loop --interval-seconds 30

    # 干运行，不实际入队
    python -m engram.logbook.cli.scm_sync scheduler --once --dry-run

    # JSON 格式输出（便于日志采集）
    python -m engram.logbook.cli.scm_sync scheduler --loop --json

环境变量:
    LOGBOOK_DSN                 数据库连接字符串（优先）
    POSTGRES_DSN                数据库连接字符串（备用）
    ENGRAM_SCM_SYNC_ENABLED     启用 SCM 同步（设为 true）
        """,
    )

    parser.add_argument(
        "--config",
        "-c",
        metavar="PATH",
        help="配置文件路径",
    )
    parser.add_argument(
        "--dsn",
        default=_get_dsn_from_env(),
        help="数据库连接字符串（默认从环境变量读取）",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="循环模式运行（持续调度）",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_SCHEDULER_INTERVAL_SECONDS,
        help=f"循环间隔秒数（默认 {DEFAULT_SCHEDULER_INTERVAL_SECONDS}，--loop 时生效）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="执行一次调度后退出（默认行为，与 --loop 互斥）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干运行模式，不实际入队",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="JSON 格式输出（--loop 模式下每轮输出单行 JSON 便于采集）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细日志输出",
    )

    args = parser.parse_args(argv)
    logger = logging.getLogger("engram.scm_sync.scheduler")
    _setup_logging(args.verbose)

    # --once 和 --loop 互斥检查
    if args.once and args.loop:
        if args.json_output:
            print(json.dumps({"error": "--once 和 --loop 不能同时使用"}))
        else:
            print("错误: --once 和 --loop 不能同时使用", file=sys.stderr)
        return 1

    # 加载配置
    from engram.logbook.config import get_config, is_scm_sync_enabled

    try:
        config = get_config(args.config)
        config.load()
    except Exception as e:
        if args.json_output:
            print(json.dumps({"error": f"配置加载失败: {e}"}))
        else:
            print(f"错误: 配置加载失败: {e}", file=sys.stderr)
        return 1

    # 检查 SCM 同步是否启用
    if not is_scm_sync_enabled(config):
        if args.json_output:
            print(
                json.dumps(
                    {
                        "error": "SCM 同步功能未启用",
                        "hint": "设置环境变量 ENGRAM_SCM_SYNC_ENABLED=true",
                    }
                )
            )
        else:
            print("错误: SCM 同步功能未启用", file=sys.stderr)
            print("提示: 设置 ENGRAM_SCM_SYNC_ENABLED=true", file=sys.stderr)
        return 1

    # 获取 DSN
    dsn = args.dsn
    if not dsn:
        dsn = config.get("postgres.dsn")

    if not dsn:
        if args.json_output:
            print(json.dumps({"error": "未提供数据库连接字符串"}))
        else:
            print("错误: 未提供数据库连接字符串", file=sys.stderr)
        return 1

    try:
        conn = _get_connection(dsn)
    except Exception as e:
        if args.json_output:
            print(json.dumps({"error": f"数据库连接失败: {e}"}))
        else:
            print(f"错误: 数据库连接失败: {e}", file=sys.stderr)
        return 1

    # 确定是否为循环模式
    loop_mode = args.loop and not args.once
    interval_seconds = args.interval_seconds

    try:
        scheduler_config = SchedulerConfig.from_config(config)
        cb_config = CircuitBreakerConfig.from_config(config)

        iteration = 0
        last_exit_code = 0

        while True:
            iteration += 1
            try:
                result = run_scheduler_tick(
                    conn,
                    scheduler_config=scheduler_config,
                    cb_config=cb_config,
                    dry_run=args.dry_run,
                    logger=logger,
                )

                # 构建输出数据
                output_data = result.to_dict()
                if loop_mode:
                    output_data["iteration"] = iteration

                if args.json_output:
                    # loop 模式下输出单行 JSON 便于日志采集
                    print(json.dumps(output_data, ensure_ascii=False))
                else:
                    if loop_mode:
                        print(f"[第 {iteration} 轮] 调度完成:")
                    else:
                        print("调度完成:")
                    print(f"  扫描仓库数: {result.repos_scanned}")
                    print(f"  候选任务数: {result.candidates_selected}")
                    print(f"  入队任务数: {result.jobs_enqueued}")
                    print(f"  跳过任务数: {result.jobs_skipped}")
                    print(f"  熔断状态: {result.circuit_state}")
                    if result.errors:
                        print(f"  错误: {len(result.errors)} 个")
                        for err in result.errors[:5]:
                            print(f"    - {err}")

                last_exit_code = 0 if not result.errors else 1

            except KeyboardInterrupt:
                logger.info("收到中断信号，退出")
                break
            except Exception as e:
                logger.error(f"调度执行错误: {e}", exc_info=True)
                if args.json_output:
                    error_data = (
                        {"error": str(e), "iteration": iteration}
                        if loop_mode
                        else {"error": str(e)}
                    )
                    print(json.dumps(error_data, ensure_ascii=False))
                last_exit_code = 1

            # 非循环模式执行一次后退出
            if not loop_mode:
                break

            # 循环模式下 sleep
            try:
                logger.debug(f"等待 {interval_seconds} 秒后执行下一轮调度...")
                time.sleep(interval_seconds)
            except KeyboardInterrupt:
                logger.info("收到中断信号，退出")
                break

        return last_exit_code

    finally:
        conn.close()


# ============ Worker CLI ============


def worker_main(argv: Optional[List[str]] = None) -> int:
    """Worker CLI 入口函数"""
    from engram.logbook.scm_sync_worker_core import process_one_job

    parser = argparse.ArgumentParser(
        description="SCM 同步 Worker - 从队列处理同步任务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 启动 worker
    python -m engram.logbook.cli.scm_sync worker --worker-id worker-1

    # 只处理一个任务
    python -m engram.logbook.cli.scm_sync worker --worker-id worker-1 --once

    # 只处理特定类型的任务
    python -m engram.logbook.cli.scm_sync worker --worker-id worker-1 --job-types commits,mrs

环境变量:
    LOGBOOK_DSN     数据库连接字符串（优先）
    POSTGRES_DSN    数据库连接字符串（备用）
        """,
    )

    parser.add_argument(
        "--worker-id",
        required=True,
        help="Worker 标识符（必填）",
    )
    parser.add_argument(
        "--dsn",
        default=_get_dsn_from_env(),
        help="数据库连接字符串（默认从环境变量读取）",
    )
    parser.add_argument(
        "--job-types",
        help="限制处理的任务类型（逗号分隔，如 commits,mrs）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只处理一个任务后退出",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=10.0,
        help="空闲时的轮询间隔（秒，默认 10）",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=300,
        help="任务租约时长（秒，默认 300）",
    )
    parser.add_argument(
        "--renew-interval",
        type=int,
        default=60,
        help="租约续期间隔（秒，默认 60）",
    )
    parser.add_argument(
        "--max-renew-failures",
        type=int,
        default=3,
        help="最大续期失败次数（默认 3）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="详细日志输出",
    )

    args = parser.parse_args(argv)
    logger = logging.getLogger("engram.scm_sync.worker")
    _setup_logging(args.verbose)

    if not args.dsn:
        logger.error("未提供数据库连接字符串。请设置 LOGBOOK_DSN 环境变量或使用 --dsn 参数")
        return 1

    job_types: Optional[List[str]] = None
    if args.job_types:
        job_types = [jt.strip() for jt in args.job_types.split(",") if jt.strip()]

    worker_cfg = {
        "lease_seconds": args.lease_seconds,
        "renew_interval_seconds": args.renew_interval,
        "max_renew_failures": args.max_renew_failures,
    }

    logger.info(f"启动 Worker: {args.worker_id}")
    logger.info(f"  lease_seconds: {worker_cfg['lease_seconds']}")
    if job_types:
        logger.info(f"  job_types: {job_types}")

    try:
        conn = _get_connection(args.dsn)
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        return 1

    processed_count = 0

    try:
        while True:
            try:
                processed = process_one_job(
                    worker_id=args.worker_id,
                    job_types=job_types,
                    worker_cfg=worker_cfg,
                    conn=conn,
                )

                if processed:
                    processed_count += 1
                    logger.debug(f"已处理 {processed_count} 个任务")

                    if args.once:
                        logger.info("--once 模式，退出")
                        break
                else:
                    if args.once:
                        logger.info("--once 模式，队列为空，退出")
                        break

                    logger.debug(f"队列为空，等待 {args.poll_interval} 秒")
                    time.sleep(args.poll_interval)

            except KeyboardInterrupt:
                logger.info("收到中断信号，退出")
                break
            except Exception as e:
                logger.error(f"处理任务时出错: {e}", exc_info=True)
                time.sleep(args.poll_interval)

        logger.info(f"Worker 退出，共处理 {processed_count} 个任务")
        return 0

    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============ Reaper CLI ============


def reaper_main(argv: Optional[List[str]] = None) -> int:
    """Reaper CLI 入口函数"""
    from engram.logbook.scm_sync_reaper_core import (
        DEFAULT_GRACE_SECONDS,
        DEFAULT_MAX_DURATION_SECONDS,
        DEFAULT_RETRY_DELAY_SECONDS,
        JobRecoveryPolicy,
        run_reaper,
    )

    parser = argparse.ArgumentParser(
        description="SCM 同步任务回收器 - 回收过期的 running 任务、runs 和 locks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 执行一次回收
    python -m engram.logbook.cli.scm_sync reaper --once

    # 循环模式运行
    python -m engram.logbook.cli.scm_sync reaper --loop --interval-seconds 60

    # 模拟运行
    python -m engram.logbook.cli.scm_sync reaper --dry-run --verbose

    # 自定义参数
    python -m engram.logbook.cli.scm_sync reaper --grace-seconds 120 --policy to_pending

    # JSON 格式输出（便于日志采集）
    python -m engram.logbook.cli.scm_sync reaper --loop --json

环境变量:
    LOGBOOK_DSN     数据库连接字符串（优先）
    POSTGRES_DSN    数据库连接字符串（备用）
        """,
    )

    parser.add_argument(
        "--dsn",
        default=_get_dsn_from_env(),
        help="数据库连接字符串（默认从环境变量读取）",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="循环模式运行（持续回收）",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_REAPER_INTERVAL_SECONDS,
        help=f"循环间隔秒数（默认 {DEFAULT_REAPER_INTERVAL_SECONDS}，--loop 时生效）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="执行一次回收后退出（默认行为，与 --loop 互斥）",
    )
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=DEFAULT_GRACE_SECONDS,
        help=f"Job 过期宽限时间，单位秒（默认 {DEFAULT_GRACE_SECONDS}）",
    )
    parser.add_argument(
        "--max-duration-seconds",
        type=int,
        default=DEFAULT_MAX_DURATION_SECONDS,
        help=f"Run 最大运行时间，单位秒（默认 {DEFAULT_MAX_DURATION_SECONDS}）",
    )
    parser.add_argument(
        "--policy",
        type=str,
        choices=["to_failed", "to_pending"],
        default="to_failed",
        help="Job 恢复策略：to_failed（默认）或 to_pending",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=DEFAULT_RETRY_DELAY_SECONDS,
        help=f"Job 失败后重试延迟，单位秒（默认 {DEFAULT_RETRY_DELAY_SECONDS}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模拟运行，不实际修改数据库",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="JSON 格式输出（--loop 模式下每轮输出单行 JSON 便于采集）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="输出详细日志",
    )

    args = parser.parse_args(argv)
    logger = logging.getLogger("engram.scm_sync.reaper")
    _setup_logging(args.verbose)

    # --once 和 --loop 互斥检查
    if args.once and args.loop:
        if args.json_output:
            print(json.dumps({"error": "--once 和 --loop 不能同时使用"}))
        else:
            print("错误: --once 和 --loop 不能同时使用", file=sys.stderr)
        return 1

    if not args.dsn:
        if args.json_output:
            print(json.dumps({"error": "未提供数据库连接字符串"}))
        else:
            logger.error("未提供数据库连接字符串。请设置 LOGBOOK_DSN 环境变量或使用 --dsn 参数")
        return 1

    policy = JobRecoveryPolicy(args.policy)

    # 确定是否为循环模式
    loop_mode = args.loop and not args.once
    interval_seconds = args.interval_seconds

    if not args.json_output:
        logger.info("开始执行 SCM Sync Reaper")
        logger.info(f"  grace_seconds: {args.grace_seconds}")
        logger.info(f"  policy: {policy.value}")
        logger.info(f"  dry_run: {args.dry_run}")
        if loop_mode:
            logger.info(f"  loop_mode: True, interval_seconds: {interval_seconds}")

    iteration = 0
    last_exit_code = 0

    while True:
        iteration += 1

        try:
            result = run_reaper(
                dsn=args.dsn,
                grace_seconds=args.grace_seconds,
                max_duration_seconds=args.max_duration_seconds,
                policy=policy,
                retry_delay_seconds=args.retry_delay,
                dry_run=args.dry_run,
                logger=logger if not args.json_output else None,
            )

            total_processed = (
                result["jobs"]["processed"]
                + result["runs"]["processed"]
                + result["locks"]["processed"]
            )
            total_errors = (
                result["jobs"]["errors"] + result["runs"]["errors"] + result["locks"]["errors"]
            )

            # 构建输出数据
            output_data = {
                "jobs": result["jobs"],
                "runs": result["runs"],
                "locks": result["locks"],
                "total_processed": total_processed,
                "total_errors": total_errors,
            }
            if loop_mode:
                output_data["iteration"] = iteration

            if args.json_output:
                # loop 模式下输出单行 JSON 便于日志采集
                print(json.dumps(output_data, ensure_ascii=False))
            else:
                if loop_mode:
                    logger.info(f"[第 {iteration} 轮] Reaper 执行完成")
                else:
                    logger.info("=" * 50)
                    logger.info("Reaper 执行完成")
                logger.info(
                    f"  Jobs:  processed={result['jobs']['processed']}, "
                    f"to_failed={result['jobs']['to_failed']}, "
                    f"to_dead={result['jobs']['to_dead']}, "
                    f"errors={result['jobs']['errors']}"
                )
                logger.info(
                    f"  Runs:  processed={result['runs']['processed']}, "
                    f"failed={result['runs']['failed']}, "
                    f"errors={result['runs']['errors']}"
                )
                logger.info(
                    f"  Locks: processed={result['locks']['processed']}, "
                    f"released={result['locks']['released']}, "
                    f"errors={result['locks']['errors']}"
                )
                logger.info(f"  Total: processed={total_processed}, errors={total_errors}")

            last_exit_code = 0 if total_errors == 0 else 1

        except KeyboardInterrupt:
            logger.info("收到中断信号，退出")
            break
        except Exception as e:
            logger.exception(f"Reaper 执行失败: {e}")
            if args.json_output:
                error_data = (
                    {"error": str(e), "iteration": iteration} if loop_mode else {"error": str(e)}
                )
                print(json.dumps(error_data, ensure_ascii=False))
            last_exit_code = 2

        # 非循环模式执行一次后退出
        if not loop_mode:
            break

        # 循环模式下 sleep
        try:
            if not args.json_output:
                logger.debug(f"等待 {interval_seconds} 秒后执行下一轮回收...")
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("收到中断信号，退出")
            break

    return last_exit_code


# ============ Status CLI ============


def status_main(argv: Optional[List[str]] = None) -> int:
    """Status CLI 入口函数"""
    from engram.logbook.scm_sync_status import (
        check_invariants,
        format_health_check_output,
        format_prometheus_metrics,
        get_sync_summary,
    )

    parser = argparse.ArgumentParser(
        description="SCM 同步状态摘要 - 查看同步健康状态与指标",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # JSON 输出（紧凑）
    python -m engram.logbook.cli.scm_sync status

    # 美化 JSON 输出
    python -m engram.logbook.cli.scm_sync status --json

    # Prometheus 指标格式
    python -m engram.logbook.cli.scm_sync status --prometheus

    # 健康检查（用于监控告警）
    python -m engram.logbook.cli.scm_sync status --health
    python -m engram.logbook.cli.scm_sync status --health --json

    # 快捷命令
    engram-scm-status --health

环境变量:
    LOGBOOK_DSN     数据库连接字符串（优先）
    POSTGRES_DSN    数据库连接字符串（备用）

退出码（--health 模式）:
    0  健康（无违规）
    1  有 warning 级别违规
    2  有 critical 级别违规
        """,
    )

    parser.add_argument(
        "--dsn",
        default=_get_dsn_from_env(),
        help="数据库连接字符串（默认从环境变量读取）",
    )

    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--json",
        dest="json_pretty",
        action="store_true",
        help="美化 JSON 输出",
    )
    output_group.add_argument(
        "--prometheus",
        action="store_true",
        help="Prometheus 指标格式输出",
    )
    output_group.add_argument(
        "--compact",
        action="store_true",
        help="紧凑 JSON 输出（默认）",
    )
    output_group.add_argument(
        "--health",
        action="store_true",
        help="执行健康不变量检查（非零退出码表示不健康）",
    )

    parser.add_argument(
        "--include-details",
        action="store_true",
        help="在健康检查中包含详细记录（与 --health 配合使用）",
    )

    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=60,
        help="Running job 过期宽限时间（秒，默认 60，与 --health 配合使用）",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="详细日志输出",
    )

    args = parser.parse_args(argv)

    if not args.dsn:
        print(
            "错误: 未提供数据库连接字符串\n请设置 LOGBOOK_DSN 环境变量或使用 --dsn 参数",
            file=sys.stderr,
        )
        return 1

    try:
        conn = _get_connection(args.dsn)
    except Exception as e:
        print(f"错误: 数据库连接失败: {e}", file=sys.stderr)
        return 1

    try:

        def json_serializer(obj):
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        # 健康检查模式
        if args.health:
            result = check_invariants(
                conn,
                include_details=args.include_details,
                grace_seconds=args.grace_seconds,
            )

            if args.json_pretty:
                print(
                    json.dumps(
                        result.to_dict(), indent=2, ensure_ascii=False, default=json_serializer
                    )
                )
            else:
                # 默认文本输出
                print(format_health_check_output(result, verbose=args.verbose))

            return result.exit_code

        # 常规状态摘要模式
        summary = get_sync_summary(conn)

        if args.prometheus:
            print(format_prometheus_metrics(summary))
        elif args.json_pretty:
            print(json.dumps(summary, indent=2, ensure_ascii=False, default=json_serializer))
        else:
            print(json.dumps(summary, ensure_ascii=False, default=json_serializer))

        return 0

    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============ Runner CLI ============


def runner_main(argv: Optional[List[str]] = None) -> int:
    """Runner CLI 入口函数

    支持增量同步和回填同步两种模式：
    - 增量同步: 从 watermark 开始同步新数据
    - 回填同步: 按时间/版本窗口分片同步历史数据

    返回码策略:
    - 0 (EXIT_SUCCESS): 全部成功
    - 1 (EXIT_PARTIAL): 部分成功（有失败但非全部失败）
    - 2 (EXIT_FAILED): 全部失败或严重错误

    Note:
        使用 engram.logbook.scm_sync_runner.create_parser() 创建解析器，
        确保所有入口点（engram-scm-sync runner、engram-scm-runner、
        python scm_sync_runner.py）使用一致的参数定义。
    """
    from datetime import datetime as dt

    from engram.logbook.scm_sync_runner import (
        EXIT_FAILED,
        EXIT_PARTIAL,
        EXIT_SUCCESS,
        BackfillConfig,
        JobSpec,
        RepoSpec,
        RunnerContext,
        SyncRunner,
        calculate_backfill_window,
        create_parser,
        get_exit_code,
    )

    logger = logging.getLogger("engram.scm_sync.runner")

    # 复用 scm_sync_runner 的解析器，确保参数定义一致
    parser = create_parser()
    args = parser.parse_args(argv)

    # 配置日志
    _setup_logging(args.verbose)

    # 加载配置
    from engram.logbook.config import get_config

    try:
        config = get_config(args.config)
    except Exception as e:
        if args.json_output:
            print(json.dumps({"error": f"配置加载失败: {e}", "exit_code": EXIT_FAILED}))
        else:
            print(f"错误: 配置加载失败: {e}", file=sys.stderr)
        return EXIT_FAILED

    # 处理 config 子命令
    if args.command == "config":
        if args.show_backfill:
            bf_config = BackfillConfig.from_config(config)
            config_dict = {
                "repair_window_hours": bf_config.repair_window_hours,
                "cron_hint": bf_config.cron_hint,
                "max_concurrent_jobs": bf_config.max_concurrent_jobs,
                "default_update_watermark": bf_config.default_update_watermark,
            }
            if args.json_output:
                print(json.dumps(config_dict, indent=2))
            else:
                print("回填配置:")
                for k, v in config_dict.items():
                    print(f"  {k}: {v}")
        return EXIT_SUCCESS

    # 解析仓库规格
    try:
        repo = RepoSpec.parse(args.repo)
    except ValueError as e:
        if args.json_output:
            print(json.dumps({"error": f"仓库规格错误: {e}", "exit_code": EXIT_FAILED}))
        else:
            print(f"错误: 仓库规格错误: {e}", file=sys.stderr)
        return EXIT_FAILED

    # 解析任务类型
    try:
        job = JobSpec.parse(args.job)
    except ValueError as e:
        if args.json_output:
            print(json.dumps({"error": f"任务类型错误: {e}", "exit_code": EXIT_FAILED}))
        else:
            print(f"错误: 任务类型错误: {e}", file=sys.stderr)
        return EXIT_FAILED

    # 构建运行器上下文
    ctx = RunnerContext(
        config=config,
        repo=repo,
        job=job,
        config_path=args.config,
        verbose=args.verbose,
        dry_run=args.dry_run,
    )

    # 处理 incremental 子命令
    if args.command == "incremental":
        runner = SyncRunner(ctx)

        # 支持循环模式
        loop_mode = args.loop
        max_iterations = args.max_iterations
        loop_interval = args.loop_interval
        iteration = 0
        last_exit_code = EXIT_SUCCESS

        while True:
            iteration += 1

            try:
                sync_result = runner.run_incremental()

                # 构建输出数据
                output_data = sync_result.to_dict()
                output_data["exit_code"] = get_exit_code(sync_result.status)
                if loop_mode:
                    output_data["iteration"] = iteration

                if args.json_output:
                    print(json.dumps(output_data, ensure_ascii=False))
                else:
                    if loop_mode:
                        logger.info(f"[第 {iteration} 轮] 同步完成: {sync_result.status}")
                    else:
                        print(f"同步完成: {sync_result.status}")
                    print(f"  仓库: {sync_result.repo}")
                    print(f"  任务: {sync_result.job}")
                    print(f"  同步数: {sync_result.items_synced}")
                    if sync_result.error:
                        print(f"  错误: {sync_result.error}")
                    if sync_result.vfacts_refreshed:
                        print("  vfacts 已刷新")

                last_exit_code = get_exit_code(sync_result.status)

            except KeyboardInterrupt:
                logger.info("收到中断信号，退出")
                break
            except Exception as e:
                logger.error(f"同步执行错误: {e}", exc_info=True)
                if args.json_output:
                    error_data = {"error": str(e), "exit_code": EXIT_FAILED}
                    if loop_mode:
                        error_data["iteration"] = iteration
                    print(json.dumps(error_data, ensure_ascii=False))
                last_exit_code = EXIT_FAILED

            # 非循环模式执行一次后退出
            if not loop_mode:
                break

            # 检查最大迭代次数
            if max_iterations > 0 and iteration >= max_iterations:
                logger.info(f"已达到最大迭代次数 {max_iterations}，退出")
                break

            # 循环模式下 sleep
            try:
                if not args.json_output:
                    logger.debug(f"等待 {loop_interval} 秒后执行下一轮同步...")
                time.sleep(loop_interval)
            except KeyboardInterrupt:
                logger.info("收到中断信号，退出")
                break

        return last_exit_code

    # 处理 backfill 子命令
    if args.command == "backfill":
        ctx.update_watermark = args.update_watermark
        runner = SyncRunner(ctx)

        # 解析时间参数
        since = None
        until = None
        start_rev = args.start_rev
        end_rev = args.end_rev

        # 如果指定了 --since 或 --until，解析 ISO8601 时间
        if args.since:
            try:
                since = dt.fromisoformat(args.since.replace("Z", "+00:00"))
            except ValueError as e:
                if args.json_output:
                    print(
                        json.dumps(
                            {"error": f"--since 时间格式错误: {e}", "exit_code": EXIT_FAILED}
                        )
                    )
                else:
                    print(f"错误: --since 时间格式错误: {e}", file=sys.stderr)
                return EXIT_FAILED

        if args.until:
            try:
                until = dt.fromisoformat(args.until.replace("Z", "+00:00"))
            except ValueError as e:
                if args.json_output:
                    print(
                        json.dumps(
                            {"error": f"--until 时间格式错误: {e}", "exit_code": EXIT_FAILED}
                        )
                    )
                else:
                    print(f"错误: --until 时间格式错误: {e}", file=sys.stderr)
                return EXIT_FAILED

        # 如果指定了 --last-hours 或 --last-days，计算时间窗口
        if args.last_hours or args.last_days:
            since, until = calculate_backfill_window(
                hours=args.last_hours,
                days=args.last_days,
            )

        try:
            # 执行回填
            backfill_result = runner.run_backfill(
                since=since,
                until=until,
                start_rev=start_rev,
                end_rev=end_rev,
            )

            # 构建输出数据
            output_data = backfill_result.to_dict()
            output_data["exit_code"] = get_exit_code(backfill_result.status)

            if args.json_output:
                print(json.dumps(output_data, ensure_ascii=False, indent=2))
            else:
                print(f"回填完成: {backfill_result.status}")
                print(f"  仓库: {backfill_result.repo}")
                print(f"  任务: {backfill_result.job}")
                print(f"  总 chunks: {backfill_result.total_chunks}")
                print(f"  成功 chunks: {backfill_result.success_chunks}")
                if backfill_result.partial_chunks > 0:
                    print(f"  部分成功 chunks: {backfill_result.partial_chunks}")
                if backfill_result.failed_chunks > 0:
                    print(f"  失败 chunks: {backfill_result.failed_chunks}")
                print(f"  总同步数: {backfill_result.total_items_synced}")
                if backfill_result.total_items_skipped > 0:
                    print(f"  总跳过数: {backfill_result.total_items_skipped}")
                if backfill_result.total_items_failed > 0:
                    print(f"  总失败数: {backfill_result.total_items_failed}")
                if backfill_result.watermark_updated:
                    print("  游标已更新")
                if backfill_result.vfacts_refreshed:
                    print("  vfacts 已刷新")
                if backfill_result.errors:
                    print(f"  错误数: {len(backfill_result.errors)}")
                    for err in backfill_result.errors[:5]:
                        print(f"    - {err}")
                    if len(backfill_result.errors) > 5:
                        print(f"    ... 还有 {len(backfill_result.errors) - 5} 个错误")

            return get_exit_code(backfill_result.status)

        except KeyboardInterrupt:
            logger.info("收到中断信号，退出")
            if args.json_output:
                print(json.dumps({"error": "interrupted", "exit_code": EXIT_PARTIAL}))
            return EXIT_PARTIAL
        except Exception as e:
            logger.error(f"回填执行错误: {e}", exc_info=True)
            if args.json_output:
                print(json.dumps({"error": str(e), "exit_code": EXIT_FAILED}))
            else:
                print(f"错误: {e}", file=sys.stderr)
            return EXIT_FAILED

    return EXIT_SUCCESS


# ============ Admin CLI ============


def _serialize_datetime(obj):
    """序列化 datetime 对象为 ISO 格式字符串"""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _redact_job_info(job: dict) -> dict:
    """对任务信息进行脱敏"""
    from engram.logbook.scm_auth import redact, redact_dict

    result = dict(job)
    # 脱敏 payload_json
    if result.get("payload_json"):
        payload = result["payload_json"]
        if isinstance(payload, str):
            import json as json_mod

            try:
                payload = json_mod.loads(payload)
            except Exception:
                payload = {}
        result["payload_json"] = redact_dict(payload) if payload else {}
    # 脱敏 last_error
    if result.get("last_error"):
        result["last_error"] = redact(result["last_error"])
    return result


def _redact_lock_info(lock: dict) -> dict:
    """对锁信息进行脱敏"""
    from engram.logbook.scm_auth import redact

    result = dict(lock)
    if result.get("locked_by"):
        result["locked_by"] = redact(result["locked_by"])
    return result


def _redact_pause_info(pause) -> dict:
    """对暂停信息进行脱敏"""
    from typing import Any

    from engram.logbook.scm_auth import redact

    result: dict[str, Any]
    if hasattr(pause, "to_dict"):
        result = pause.to_dict()
    else:
        result = dict(pause)
    if result.get("reason"):
        result["reason"] = redact(result["reason"])
    return result


def _redact_cursor_info(cursor: dict) -> dict:
    """对游标信息进行脱敏"""
    from engram.logbook.scm_auth import redact_dict

    result = dict(cursor)
    if result.get("value_json"):
        value = result["value_json"]
        if isinstance(value, str):
            import json as json_mod

            try:
                value = json_mod.loads(value)
            except Exception:
                value = {}
        result["value_json"] = redact_dict(value) if value else {}
    if result.get("value"):
        value = result["value"]
        if isinstance(value, dict):
            result["value"] = redact_dict(value)
    return result


def admin_main(argv: Optional[List[str]] = None) -> int:
    """Admin CLI 入口函数

    提供 SCM Sync 子系统的管理命令，包括：
    - jobs: 任务管理 (list/reset-dead/mark-dead)
    - locks: 锁管理 (list/force-release/list-expired)
    - pauses: 暂停管理 (set/unset/list)
    - cursors: 游标管理 (list/get/set/delete)
    - rate-limit: 速率限制管理 (buckets list/pause/unpause)
    """
    parser = argparse.ArgumentParser(
        prog="engram-scm-sync admin",
        description="SCM Sync 管理命令 - 运维管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
子命令:
    jobs        任务管理 (list/reset-dead/mark-dead)
    locks       锁管理 (list/force-release/list-expired)
    pauses      暂停管理 (set/unset/list)
    cursors     游标管理 (list/get/set/delete)
    rate-limit  速率限制管理 (buckets list/pause/unpause)

示例:
    # 列出 dead 任务
    engram-scm-sync admin jobs list --status dead

    # 重置所有 dead 任务
    engram-scm-sync admin jobs reset-dead

    # 列出所有锁
    engram-scm-sync admin locks list

    # 列出所有暂停
    engram-scm-sync admin pauses list

    # 列出所有游标
    engram-scm-sync admin cursors list

    # 列出速率限制桶
    engram-scm-sync admin rate-limit buckets list

详细帮助:
    engram-scm-sync admin <子命令> --help
        """,
    )

    parser.add_argument(
        "--dsn",
        default=_get_dsn_from_env(),
        help="数据库连接字符串（默认从环境变量读取）",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="JSON 格式输出",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="详细日志输出",
    )

    subparsers = parser.add_subparsers(dest="admin_command", help="管理子命令")

    # ===== jobs 子命令 =====
    jobs_parser = subparsers.add_parser("jobs", help="任务管理")
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_action", help="任务操作")

    # jobs list
    jobs_list = jobs_sub.add_parser("list", help="列出任务")
    jobs_list.add_argument("--status", default="dead", help="任务状态 (默认: dead)")
    jobs_list.add_argument("--repo-id", type=int, help="按仓库 ID 过滤")
    jobs_list.add_argument("--job-type", help="按任务类型过滤")
    jobs_list.add_argument("--limit", type=int, default=100, help="返回数量限制")

    # jobs reset-dead
    jobs_reset = jobs_sub.add_parser("reset-dead", help="重置 dead 任务为 pending")
    jobs_reset.add_argument("--job-ids", help="任务 ID 列表（逗号分隔）")
    jobs_reset.add_argument("--repo-id", type=int, help="按仓库 ID 过滤")
    jobs_reset.add_argument("--limit", type=int, default=100, help="重置数量限制")
    jobs_reset.add_argument("--dry-run", action="store_true", help="模拟运行")

    # jobs mark-dead
    jobs_mark = jobs_sub.add_parser("mark-dead", help="将任务标记为 dead")
    jobs_mark.add_argument("--job-id", required=True, help="任务 ID")
    jobs_mark.add_argument("--reason", default="manual_mark_dead", help="标记原因")

    # ===== locks 子命令 =====
    locks_parser = subparsers.add_parser("locks", help="锁管理")
    locks_sub = locks_parser.add_subparsers(dest="locks_action", help="锁操作")

    # locks list
    locks_list = locks_sub.add_parser("list", help="列出所有锁")
    locks_list.add_argument("--repo-id", type=int, help="按仓库 ID 过滤")
    locks_list.add_argument("--limit", type=int, default=100, help="返回数量限制")

    # locks force-release
    locks_release = locks_sub.add_parser("force-release", help="强制释放锁")
    locks_release.add_argument("--lock-id", type=int, required=True, help="锁 ID")

    # locks list-expired
    locks_expired = locks_sub.add_parser("list-expired", help="列出过期锁")
    locks_expired.add_argument("--grace-seconds", type=int, default=0, help="宽限时间（秒）")
    locks_expired.add_argument("--limit", type=int, default=100, help="返回数量限制")

    # ===== pauses 子命令 =====
    pauses_parser = subparsers.add_parser("pauses", help="暂停管理")
    pauses_sub = pauses_parser.add_subparsers(dest="pauses_action", help="暂停操作")

    # pauses list
    pauses_list = pauses_sub.add_parser("list", help="列出所有暂停")
    pauses_list.add_argument("--include-expired", action="store_true", help="包含已过期的暂停")

    # pauses set
    pauses_set = pauses_sub.add_parser("set", help="设置暂停")
    pauses_set.add_argument("--repo-id", type=int, required=True, help="仓库 ID")
    pauses_set.add_argument("--job-type", required=True, help="任务类型")
    pauses_set.add_argument("--duration", type=int, required=True, help="暂停时长（秒）")
    pauses_set.add_argument("--reason", default="manual_pause", help="暂停原因")

    # pauses unset
    pauses_unset = pauses_sub.add_parser("unset", help="取消暂停")
    pauses_unset.add_argument("--repo-id", type=int, required=True, help="仓库 ID")
    pauses_unset.add_argument("--job-type", required=True, help="任务类型")

    # ===== cursors 子命令 =====
    cursors_parser = subparsers.add_parser("cursors", help="游标管理")
    cursors_sub = cursors_parser.add_subparsers(dest="cursors_action", help="游标操作")

    # cursors list
    cursors_list = cursors_sub.add_parser("list", help="列出所有游标")
    cursors_list.add_argument("--key-prefix", help="按 key 前缀过滤")
    cursors_list.add_argument("--limit", type=int, default=200, help="返回数量限制")

    # cursors get
    cursors_get = cursors_sub.add_parser("get", help="获取游标值")
    cursors_get.add_argument("--repo-id", type=int, required=True, help="仓库 ID")
    cursors_get.add_argument("--job-type", required=True, help="任务类型")

    # cursors set
    cursors_set = cursors_sub.add_parser("set", help="设置游标值")
    cursors_set.add_argument("--repo-id", type=int, required=True, help="仓库 ID")
    cursors_set.add_argument("--job-type", required=True, help="任务类型")
    cursors_set.add_argument("--value", required=True, help="游标值（JSON 格式）")

    # cursors delete
    cursors_delete = cursors_sub.add_parser("delete", help="删除游标")
    cursors_delete.add_argument("--repo-id", type=int, required=True, help="仓库 ID")
    cursors_delete.add_argument("--job-type", required=True, help="任务类型")

    # ===== rate-limit 子命令 =====
    ratelimit_parser = subparsers.add_parser("rate-limit", help="速率限制管理")
    ratelimit_sub = ratelimit_parser.add_subparsers(dest="ratelimit_action", help="速率限制操作")

    # rate-limit buckets
    buckets_parser = ratelimit_sub.add_parser("buckets", help="桶管理")
    buckets_sub = buckets_parser.add_subparsers(dest="buckets_action", help="桶操作")

    # buckets list
    buckets_list = buckets_sub.add_parser("list", help="列出所有桶")
    buckets_list.add_argument("--limit", type=int, default=100, help="返回数量限制")

    # buckets pause
    buckets_pause = buckets_sub.add_parser("pause", help="暂停桶")
    buckets_pause.add_argument("--instance-key", required=True, help="实例 key")
    buckets_pause.add_argument("--duration", type=int, required=True, help="暂停时长（秒）")
    buckets_pause.add_argument("--reason", default="manual_pause", help="暂停原因")

    # buckets unpause
    buckets_unpause = buckets_sub.add_parser("unpause", help="取消暂停桶")
    buckets_unpause.add_argument("--instance-key", required=True, help="实例 key")

    # 解析参数
    if argv is None:
        argv = sys.argv[1:]

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if not args.dsn:
        if args.json_output:
            print(json.dumps({"error": "未提供数据库连接字符串"}))
        else:
            print(
                "错误: 未提供数据库连接字符串。请设置 LOGBOOK_DSN 环境变量或使用 --dsn 参数",
                file=sys.stderr,
            )
        return 1

    if not args.admin_command:
        parser.print_help()
        return 0

    try:
        conn = _get_connection(args.dsn)
    except Exception as e:
        if args.json_output:
            print(json.dumps({"error": f"数据库连接失败: {e}"}))
        else:
            print(f"错误: 数据库连接失败: {e}", file=sys.stderr)
        return 1

    try:
        return _dispatch_admin_command(args, conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _dispatch_admin_command(args: argparse.Namespace, conn) -> int:
    """分发 admin 子命令"""
    from engram.logbook import scm_db

    if args.admin_command == "jobs":
        return _handle_jobs_command(args, conn, scm_db)
    elif args.admin_command == "locks":
        return _handle_locks_command(args, conn, scm_db)
    elif args.admin_command == "pauses":
        return _handle_pauses_command(args, conn, scm_db)
    elif args.admin_command == "cursors":
        return _handle_cursors_command(args, conn, scm_db)
    elif args.admin_command == "rate-limit":
        return _handle_ratelimit_command(args, conn, scm_db)
    else:
        if args.json_output:
            print(json.dumps({"error": f"未知命令: {args.admin_command}"}))
        else:
            print(f"错误: 未知命令: {args.admin_command}", file=sys.stderr)
        return 1


def _handle_jobs_command(args: argparse.Namespace, conn, scm_db) -> int:
    """处理 jobs 子命令"""
    if args.jobs_action == "list":
        jobs = scm_db.list_jobs_by_status(
            conn,
            status=args.status,
            repo_id=args.repo_id,
            job_type=args.job_type,
            limit=args.limit,
        )
        # 脱敏处理
        jobs = [_redact_job_info(j) for j in jobs]

        if args.json_output:
            print(
                json.dumps(
                    {"jobs": jobs, "count": len(jobs)},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"任务列表 (status={args.status}, count={len(jobs)}):")
            for j in jobs:
                print(
                    f"  job_id={j['job_id']}, repo_id={j['repo_id']}, job_type={j['job_type']}, "
                    f"attempts={j['attempts']}, error={j.get('last_error', '-')[:50] if j.get('last_error') else '-'}"
                )
        return 0

    elif args.jobs_action == "reset-dead":
        job_ids = None
        if args.job_ids:
            job_ids = [jid.strip() for jid in args.job_ids.split(",") if jid.strip()]

        if args.dry_run:
            # 只列出将被重置的任务
            jobs = scm_db.list_jobs_by_status(
                conn,
                status="dead",
                repo_id=args.repo_id,
                limit=args.limit,
            )
            if job_ids:
                jobs = [j for j in jobs if str(j["job_id"]) in job_ids]

            jobs = [_redact_job_info(j) for j in jobs]

            if args.json_output:
                print(
                    json.dumps(
                        {"dry_run": True, "would_reset": jobs, "count": len(jobs)},
                        default=_serialize_datetime,
                        ensure_ascii=False,
                    )
                )
            else:
                print(f"[DRY-RUN] 将重置 {len(jobs)} 个 dead 任务:")
                for j in jobs:
                    print(
                        f"  job_id={j['job_id']}, repo_id={j['repo_id']}, job_type={j['job_type']}"
                    )
            return 0

        # 执行重置
        reset_jobs = scm_db.reset_dead_jobs(
            conn,
            job_ids=job_ids,
            repo_id=args.repo_id,
            limit=args.limit,
        )
        conn.commit()

        reset_jobs = [_redact_job_info(j) for j in reset_jobs]

        if args.json_output:
            print(
                json.dumps(
                    {"reset_count": len(reset_jobs), "reset_jobs": reset_jobs},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"已重置 {len(reset_jobs)} 个 dead 任务为 pending:")
            for j in reset_jobs:
                print(f"  job_id={j['job_id']}, repo_id={j['repo_id']}, job_type={j['job_type']}")
        return 0

    elif args.jobs_action == "mark-dead":
        success = scm_db.mark_job_dead(conn, args.job_id, reason=args.reason)
        conn.commit()

        if args.json_output:
            print(json.dumps({"success": success, "job_id": args.job_id, "reason": args.reason}))
        else:
            if success:
                print(f"已将任务 {args.job_id} 标记为 dead (reason={args.reason})")
            else:
                print(f"标记失败: 任务 {args.job_id} 不存在或状态不允许标记")
        return 0 if success else 1

    else:
        if args.json_output:
            print(json.dumps({"error": "请指定 jobs 子命令: list/reset-dead/mark-dead"}))
        else:
            print("错误: 请指定 jobs 子命令: list/reset-dead/mark-dead", file=sys.stderr)
        return 1


def _handle_locks_command(args: argparse.Namespace, conn, scm_db) -> int:
    """处理 locks 子命令"""
    if args.locks_action == "list":
        locks = scm_db.list_sync_locks(conn, repo_id=args.repo_id, limit=args.limit)
        locks = [_redact_lock_info(lock) for lock in locks]

        if args.json_output:
            print(
                json.dumps(
                    {"locks": locks, "count": len(locks)},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"锁列表 (count={len(locks)}):")
            for lock in locks:
                status = (
                    "EXPIRED"
                    if lock.get("is_expired")
                    else ("LOCKED" if lock.get("is_locked") else "FREE")
                )
                print(
                    f"  lock_id={lock['lock_id']}, repo_id={lock['repo_id']}, job_type={lock['job_type']}, "
                    f"status={status}, locked_by={lock.get('locked_by', '-')}"
                )
        return 0

    elif args.locks_action == "force-release":
        success = scm_db.force_release_lock(conn, args.lock_id)
        conn.commit()

        if args.json_output:
            print(json.dumps({"success": success, "lock_id": args.lock_id}))
        else:
            if success:
                print(f"已强制释放锁 lock_id={args.lock_id}")
            else:
                print(f"释放失败: 锁 lock_id={args.lock_id} 不存在")
        return 0 if success else 1

    elif args.locks_action == "list-expired":
        locks = scm_db.list_expired_locks(conn, grace_seconds=args.grace_seconds, limit=args.limit)
        locks = [_redact_lock_info(lock) for lock in locks]

        if args.json_output:
            print(
                json.dumps(
                    {"expired_locks": locks, "count": len(locks)},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"过期锁列表 (grace_seconds={args.grace_seconds}, count={len(locks)}):")
            for lock in locks:
                print(
                    f"  lock_id={lock['lock_id']}, repo_id={lock['repo_id']}, job_type={lock['job_type']}, "
                    f"locked_by={lock.get('locked_by', '-')}, locked_at={lock.get('locked_at', '-')}"
                )
        return 0

    else:
        if args.json_output:
            print(json.dumps({"error": "请指定 locks 子命令: list/force-release/list-expired"}))
        else:
            print("错误: 请指定 locks 子命令: list/force-release/list-expired", file=sys.stderr)
        return 1


def _handle_pauses_command(args: argparse.Namespace, conn, scm_db) -> int:
    """处理 pauses 子命令"""
    if args.pauses_action == "list":
        pauses = scm_db.list_all_pauses(conn, include_expired=args.include_expired)
        pauses = [_redact_pause_info(p) for p in pauses]

        if args.json_output:
            print(
                json.dumps(
                    {"pauses": pauses, "count": len(pauses)},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"暂停列表 (include_expired={args.include_expired}, count={len(pauses)}):")
            for p in pauses:
                print(
                    f"  repo_id={p['repo_id']}, job_type={p['job_type']}, "
                    f"reason_code={p.get('reason_code', '-')}, reason={p.get('reason', '-')[:30]}"
                )
        return 0

    elif args.pauses_action == "set":
        pause = scm_db.set_repo_job_pause(
            conn,
            repo_id=args.repo_id,
            job_type=args.job_type,
            pause_duration_seconds=args.duration,
            reason=args.reason,
        )
        conn.commit()

        pause_dict = _redact_pause_info(pause)

        if args.json_output:
            print(
                json.dumps(
                    {"success": True, "pause": pause_dict},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            print(
                f"已设置暂停: repo_id={args.repo_id}, job_type={args.job_type}, duration={args.duration}s"
            )
        return 0

    elif args.pauses_action == "unset":
        success = scm_db.unset_repo_job_pause(conn, repo_id=args.repo_id, job_type=args.job_type)
        conn.commit()

        if args.json_output:
            print(
                json.dumps({"success": success, "repo_id": args.repo_id, "job_type": args.job_type})
            )
        else:
            if success:
                print(f"已取消暂停: repo_id={args.repo_id}, job_type={args.job_type}")
            else:
                print(
                    f"取消失败: 暂停记录不存在 (repo_id={args.repo_id}, job_type={args.job_type})"
                )
        return 0 if success else 1

    else:
        if args.json_output:
            print(json.dumps({"error": "请指定 pauses 子命令: list/set/unset"}))
        else:
            print("错误: 请指定 pauses 子命令: list/set/unset", file=sys.stderr)
        return 1


def _handle_cursors_command(args: argparse.Namespace, conn, scm_db) -> int:
    """处理 cursors 子命令"""
    if args.cursors_action == "list":
        cursors = scm_db.list_kv_cursors(conn, key_prefix=args.key_prefix, limit=args.limit)
        cursors = [_redact_cursor_info(c) for c in cursors]

        if args.json_output:
            print(
                json.dumps(
                    {"cursors": cursors, "count": len(cursors)},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"游标列表 (count={len(cursors)}):")
            for c in cursors:
                print(f"  key={c['key']}, updated_at={c.get('updated_at', '-')}")
        return 0

    elif args.cursors_action == "get":
        cursor = scm_db.get_cursor_value(conn, args.repo_id, args.job_type)
        if cursor:
            cursor = _redact_cursor_info(cursor)

        if args.json_output:
            if cursor:
                print(
                    json.dumps(
                        {"found": True, "cursor": cursor},
                        default=_serialize_datetime,
                        ensure_ascii=False,
                    )
                )
            else:
                print(
                    json.dumps({"found": False, "repo_id": args.repo_id, "job_type": args.job_type})
                )
        else:
            if cursor:
                print(f"游标 (repo_id={args.repo_id}, job_type={args.job_type}):")
                print(f"  value: {cursor.get('value', '-')}")
                print(f"  updated_at: {cursor.get('updated_at', '-')}")
            else:
                print(f"游标不存在: repo_id={args.repo_id}, job_type={args.job_type}")
        return 0 if cursor else 1

    elif args.cursors_action == "set":
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError as e:
            if args.json_output:
                print(json.dumps({"error": f"无效的 JSON 值: {e}"}))
            else:
                print(f"错误: 无效的 JSON 值: {e}", file=sys.stderr)
            return 1

        success = scm_db.set_cursor_value(conn, args.repo_id, args.job_type, value)
        conn.commit()

        if args.json_output:
            print(
                json.dumps({"success": success, "repo_id": args.repo_id, "job_type": args.job_type})
            )
        else:
            if success:
                print(f"已设置游标: repo_id={args.repo_id}, job_type={args.job_type}")
            else:
                print(f"设置失败: repo_id={args.repo_id}, job_type={args.job_type}")
        return 0 if success else 1

    elif args.cursors_action == "delete":
        success = scm_db.delete_cursor_value(conn, args.repo_id, args.job_type)
        conn.commit()

        if args.json_output:
            print(
                json.dumps({"success": success, "repo_id": args.repo_id, "job_type": args.job_type})
            )
        else:
            if success:
                print(f"已删除游标: repo_id={args.repo_id}, job_type={args.job_type}")
            else:
                print(f"删除失败: 游标不存在 (repo_id={args.repo_id}, job_type={args.job_type})")
        return 0 if success else 1

    else:
        if args.json_output:
            print(json.dumps({"error": "请指定 cursors 子命令: list/get/set/delete"}))
        else:
            print("错误: 请指定 cursors 子命令: list/get/set/delete", file=sys.stderr)
        return 1


def _handle_ratelimit_command(args: argparse.Namespace, conn, scm_db) -> int:
    """处理 rate-limit 子命令"""
    if args.ratelimit_action == "buckets":
        return _handle_buckets_command(args, conn, scm_db)
    else:
        if args.json_output:
            print(json.dumps({"error": "请指定 rate-limit 子命令: buckets"}))
        else:
            print("错误: 请指定 rate-limit 子命令: buckets", file=sys.stderr)
        return 1


def _handle_buckets_command(args: argparse.Namespace, conn, scm_db) -> int:
    """处理 rate-limit buckets 子命令"""
    if args.buckets_action == "list":
        buckets = scm_db.list_rate_limit_buckets(conn, limit=args.limit)

        if args.json_output:
            print(
                json.dumps(
                    {"buckets": buckets, "count": len(buckets)},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"速率限制桶列表 (count={len(buckets)}):")
            for b in buckets:
                status = "PAUSED" if b.get("is_paused") else "ACTIVE"
                remaining = b.get("remaining_pause_seconds", 0)
                print(
                    f"  instance_key={b['instance_key']}, status={status}, "
                    f"tokens={b.get('tokens', '-')}, remaining_pause={remaining:.0f}s"
                )
        return 0

    elif args.buckets_action == "pause":
        result = scm_db.pause_rate_limit_bucket(
            conn,
            args.instance_key,
            args.duration,
            reason=args.reason,
        )
        conn.commit()

        if args.json_output:
            print(
                json.dumps(
                    {"success": True, "result": result},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"已暂停桶: instance_key={args.instance_key}, duration={args.duration}s")
        return 0

    elif args.buckets_action == "unpause":
        result = scm_db.unpause_rate_limit_bucket(conn, args.instance_key)
        conn.commit()

        if args.json_output:
            print(
                json.dumps(
                    {"success": True, "result": result},
                    default=_serialize_datetime,
                    ensure_ascii=False,
                )
            )
        else:
            if result.get("status") == "not_found":
                print(f"取消暂停失败: 桶不存在 (instance_key={args.instance_key})")
            else:
                print(f"已取消暂停: instance_key={args.instance_key}")
        return 0 if result.get("status") != "not_found" else 1

    else:
        if args.json_output:
            print(json.dumps({"error": "请指定 buckets 子命令: list/pause/unpause"}))
        else:
            print("错误: 请指定 buckets 子命令: list/pause/unpause", file=sys.stderr)
        return 1


# ============ 统一入口 ============


def main(argv: Optional[List[str]] = None) -> int:
    """SCM Sync 子系统统一 CLI 入口"""
    parser = argparse.ArgumentParser(
        prog="engram-scm-sync",
        description="SCM Sync 子系统 - 管理 SCM 同步的调度、执行、清理与状态查询",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
子命令:
    scheduler   调度器 - 扫描仓库并入队同步任务
    worker      Worker - 从队列获取并执行同步任务
    reaper      清理器 - 回收过期任务、runs 和锁
    status      状态查询 - 查看同步健康状态与指标
    runner      运行器 - 增量同步与回填工具
    admin       管理命令 - 运维管理工具 (jobs/locks/pauses/cursors/rate-limit)

示例:
    python -m engram.logbook.cli.scm_sync scheduler --once
    python -m engram.logbook.cli.scm_sync worker --worker-id worker-1
    python -m engram.logbook.cli.scm_sync reaper --dry-run
    python -m engram.logbook.cli.scm_sync status --json
    python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123
    python -m engram.logbook.cli.scm_sync admin jobs list --status dead
    python -m engram.logbook.cli.scm_sync admin locks list-expired
    python -m engram.logbook.cli.scm_sync admin pauses list
    python -m engram.logbook.cli.scm_sync admin cursors list
    python -m engram.logbook.cli.scm_sync admin rate-limit buckets list

详细帮助:
    python -m engram.logbook.cli.scm_sync <子命令> --help
        """,
    )
    parser.add_argument("--version", action="version", version="engram-scm-sync 0.1.0")

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # scheduler 子命令
    subparsers.add_parser("scheduler", help="调度器 - 扫描仓库并入队同步任务", add_help=False)

    # worker 子命令
    subparsers.add_parser("worker", help="Worker - 从队列获取并执行同步任务", add_help=False)

    # reaper 子命令
    subparsers.add_parser("reaper", help="清理器 - 回收过期任务、runs 和锁", add_help=False)

    # status 子命令
    subparsers.add_parser("status", help="状态查询 - 查看同步健康状态与指标", add_help=False)

    # runner 子命令
    subparsers.add_parser("runner", help="运行器 - 增量同步与回填工具", add_help=False)

    # admin 子命令
    subparsers.add_parser(
        "admin",
        help="管理命令 - 运维管理工具 (jobs/locks/pauses/cursors/rate-limit)",
        add_help=False,
    )

    # 解析子命令
    if argv is None:
        argv = sys.argv[1:]

    # 找到子命令位置
    command_idx = -1
    for i, arg in enumerate(argv):
        if arg in ("scheduler", "worker", "reaper", "status", "runner", "admin"):
            command_idx = i
            break

    if command_idx == -1:
        parser.print_help()
        return 0

    command = argv[command_idx]
    remaining_args = argv[command_idx + 1 :]

    if command == "scheduler":
        return scheduler_main(remaining_args)
    elif command == "worker":
        return worker_main(remaining_args)
    elif command == "reaper":
        return reaper_main(remaining_args)
    elif command == "status":
        return status_main(remaining_args)
    elif command == "runner":
        return runner_main(remaining_args)
    elif command == "admin":
        return admin_main(remaining_args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
