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
                    error_data = {"error": str(e), "iteration": iteration} if loop_mode else {"error": str(e)}
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
                result["jobs"]["processed"] + result["runs"]["processed"] + result["locks"]["processed"]
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
                error_data = {"error": str(e), "iteration": iteration} if loop_mode else {"error": str(e)}
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
        summary = get_sync_summary(conn)

        def json_serializer(obj):
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

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
    """
    from datetime import datetime as dt

    from engram.logbook.scm_sync_runner import (
        DEFAULT_LOOP_INTERVAL_SECONDS,
        EXIT_FAILED,
        EXIT_PARTIAL,
        EXIT_SUCCESS,
        JOB_TYPE_COMMITS,
        VALID_JOB_TYPES,
        AggregatedResult,
        BackfillConfig,
        JobSpec,
        RepoSpec,
        RunnerContext,
        RunnerStatus,
        SyncRunner,
        calculate_backfill_window,
        get_exit_code,
    )

    logger = logging.getLogger("engram.scm_sync.runner")

    parser = argparse.ArgumentParser(
        description="SCM sync runner - 增量同步与回填工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 增量同步
    python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123

    # 回填最近 24 小时
    python -m engram.logbook.cli.scm_sync runner backfill --repo gitlab:123 --last-hours 24

    # 回填指定时间范围
    python -m engram.logbook.cli.scm_sync runner backfill --repo gitlab:123 \\
        --since 2025-01-01T00:00:00Z --until 2025-01-31T23:59:59Z

    # SVN 回填指定版本范围
    python -m engram.logbook.cli.scm_sync runner backfill --repo svn:https://svn.example.com/repo \\
        --start-rev 100 --end-rev 500

    # 回填并更新游标
    python -m engram.logbook.cli.scm_sync runner backfill --repo gitlab:123 --last-hours 24 --update-watermark

    # 查看回填配置
    python -m engram.logbook.cli.scm_sync runner config --show-backfill

返回码:
    0  成功 (全部 chunk 成功)
    1  部分成功 (部分 chunk 失败)
    2  失败 (全部 chunk 失败或严重错误)
        """,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志输出")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行，不执行实际操作")
    parser.add_argument("--config", metavar="PATH", help="配置文件路径")
    parser.add_argument("--json", dest="json_output", action="store_true", help="JSON 格式输出")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # incremental 子命令
    inc = subparsers.add_parser("incremental", help="增量同步")
    inc.add_argument("--repo", required=True, help="仓库规格 (格式: <type>:<id>，如 gitlab:123)")
    inc.add_argument(
        "--job",
        default=JOB_TYPE_COMMITS,
        choices=sorted(VALID_JOB_TYPES),
        help=f"任务类型 (默认: {JOB_TYPE_COMMITS})",
    )
    inc.add_argument("--loop", action="store_true", help="循环模式")
    inc.add_argument(
        "--loop-interval",
        type=int,
        default=DEFAULT_LOOP_INTERVAL_SECONDS,
        help=f"循环间隔秒数 (默认: {DEFAULT_LOOP_INTERVAL_SECONDS})",
    )
    inc.add_argument("--max-iterations", type=int, default=0, help="最大迭代次数 (0=无限)")

    # backfill 子命令
    bf = subparsers.add_parser("backfill", help="回填同步")
    bf.add_argument("--repo", required=True, help="仓库规格 (格式: <type>:<id>)")
    bf.add_argument(
        "--job",
        default=JOB_TYPE_COMMITS,
        choices=sorted(VALID_JOB_TYPES),
        help=f"任务类型 (默认: {JOB_TYPE_COMMITS})",
    )
    time_group = bf.add_mutually_exclusive_group()
    time_group.add_argument("--last-hours", type=int, help="回填最近 N 小时")
    time_group.add_argument("--last-days", type=int, help="回填最近 N 天")
    bf.add_argument("--update-watermark", action="store_true", help="更新游标位置")
    bf.add_argument("--start-rev", type=int, help="起始版本号 (SVN)")
    bf.add_argument("--end-rev", type=int, help="结束版本号 (SVN)")
    bf.add_argument("--since", help="开始时间 (ISO8601)")
    bf.add_argument("--until", help="结束时间 (ISO8601)")

    # config 子命令
    cfg = subparsers.add_parser("config", help="显示配置")
    cfg.add_argument("--show-backfill", action="store_true", help="显示回填配置")

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
            result = {
                "repair_window_hours": bf_config.repair_window_hours,
                "cron_hint": bf_config.cron_hint,
                "max_concurrent_jobs": bf_config.max_concurrent_jobs,
                "default_update_watermark": bf_config.default_update_watermark,
            }
            if args.json_output:
                print(json.dumps(result, indent=2))
            else:
                print("回填配置:")
                for k, v in result.items():
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
                result = runner.run_incremental()

                # 构建输出数据
                output_data = result.to_dict()
                output_data["exit_code"] = get_exit_code(result.status)
                if loop_mode:
                    output_data["iteration"] = iteration

                if args.json_output:
                    print(json.dumps(output_data, ensure_ascii=False))
                else:
                    if loop_mode:
                        logger.info(f"[第 {iteration} 轮] 同步完成: {result.status}")
                    else:
                        print(f"同步完成: {result.status}")
                    print(f"  仓库: {result.repo}")
                    print(f"  任务: {result.job}")
                    print(f"  同步数: {result.items_synced}")
                    if result.error:
                        print(f"  错误: {result.error}")
                    if result.vfacts_refreshed:
                        print("  vfacts 已刷新")

                last_exit_code = get_exit_code(result.status)

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
                    print(json.dumps({"error": f"--since 时间格式错误: {e}", "exit_code": EXIT_FAILED}))
                else:
                    print(f"错误: --since 时间格式错误: {e}", file=sys.stderr)
                return EXIT_FAILED

        if args.until:
            try:
                until = dt.fromisoformat(args.until.replace("Z", "+00:00"))
            except ValueError as e:
                if args.json_output:
                    print(json.dumps({"error": f"--until 时间格式错误: {e}", "exit_code": EXIT_FAILED}))
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
            result = runner.run_backfill(
                since=since,
                until=until,
                start_rev=start_rev,
                end_rev=end_rev,
            )

            # 构建输出数据
            output_data = result.to_dict()
            output_data["exit_code"] = get_exit_code(result.status)

            if args.json_output:
                print(json.dumps(output_data, ensure_ascii=False, indent=2))
            else:
                print(f"回填完成: {result.status}")
                print(f"  仓库: {result.repo}")
                print(f"  任务: {result.job}")
                print(f"  总 chunks: {result.total_chunks}")
                print(f"  成功 chunks: {result.success_chunks}")
                if result.partial_chunks > 0:
                    print(f"  部分成功 chunks: {result.partial_chunks}")
                if result.failed_chunks > 0:
                    print(f"  失败 chunks: {result.failed_chunks}")
                print(f"  总同步数: {result.total_items_synced}")
                if result.total_items_skipped > 0:
                    print(f"  总跳过数: {result.total_items_skipped}")
                if result.total_items_failed > 0:
                    print(f"  总失败数: {result.total_items_failed}")
                if result.watermark_updated:
                    print("  游标已更新")
                if result.vfacts_refreshed:
                    print("  vfacts 已刷新")
                if result.errors:
                    print(f"  错误数: {len(result.errors)}")
                    for err in result.errors[:5]:
                        print(f"    - {err}")
                    if len(result.errors) > 5:
                        print(f"    ... 还有 {len(result.errors) - 5} 个错误")

            return get_exit_code(result.status)

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

示例:
    python -m engram.logbook.cli.scm_sync scheduler --once
    python -m engram.logbook.cli.scm_sync worker --worker-id worker-1
    python -m engram.logbook.cli.scm_sync reaper --dry-run
    python -m engram.logbook.cli.scm_sync status --json
    python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123

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

    # 解析子命令
    if argv is None:
        argv = sys.argv[1:]

    # 找到子命令位置
    command_idx = -1
    for i, arg in enumerate(argv):
        if arg in ("scheduler", "worker", "reaper", "status", "runner"):
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
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
