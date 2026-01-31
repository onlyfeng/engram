"""
Outbox Worker：处理 logbook.outbox_memory 队列
负责：批量领取 pending 记录，调用 OpenMemory 写入，处理成功/失败/重试逻辑

使用 Lease 协议：
1. Worker 调用 claim_outbox(worker_id, limit, lease_seconds) 获取任务
2. 处理成功后调用 ack_sent(outbox_id, worker_id, memory_id)
3. 可重试失败调用 fail_retry(outbox_id, worker_id, error)
4. 不可恢复失败调用 mark_dead(outbox_id, worker_id, error)

用法：
    python -m gateway.outbox_worker --once    # 执行一轮后退出
    python -m gateway.outbox_worker --loop    # 持续轮询（需配合守护进程管理）
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg

from . import logbook_adapter, openmemory_client
from .audit_event import build_evidence_refs_json, build_outbox_worker_audit_event

# 导入统一错误码
try:
    from engram.logbook.errors import ErrorCode
except ImportError:
    print(
        "\n"
        "=" * 60 + "\n"
        "[ERROR] 缺少依赖: engram_logbook\n"
        "=" * 60 + "\n"
        "\n"
        "Outbox Worker 依赖 engram_logbook 模块（统一错误码等），请先安装：\n"
        "\n"
        '  pip install -e ".[full]"\n'
        "\n"
        "  # 或 Docker 环境（已自动安装）\n"
        "  docker compose -f docker-compose.unified.yml up outbox_worker\n"
        "\n"
        "=" * 60 + "\n"
    )
    sys.exit(1)

logger = logging.getLogger(__name__)


def _is_db_timeout_error(exc: BaseException) -> bool:
    """
    判断异常是否为数据库语句超时错误。

    PostgreSQL statement_timeout 会产生 SQLSTATE 57014 (query_canceled) 错误。
    """
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return True
    # 通过 SQLSTATE 检查
    if hasattr(exc, "sqlstate") and exc.sqlstate == "57014":
        return True
    # 通过 pgcode 检查 (psycopg2 兼容)
    if hasattr(exc, "pgcode") and exc.pgcode == "57014":
        return True
    return False


def _classify_db_error(exc: BaseException) -> tuple:
    """
    分类数据库错误类型。

    Returns:
        tuple: (error_type, error_reason)
        - error_type: 'db_timeout' 或 'db_error'
        - error_reason: 对应的 ErrorCode 常量
    """
    if _is_db_timeout_error(exc):
        return "db_timeout", ErrorCode.OUTBOX_FLUSH_DB_TIMEOUT
    return "db_error", ErrorCode.OUTBOX_FLUSH_DB_ERROR


# ---------- 配置 ----------


@dataclass
class WorkerConfig:
    """Worker 配置"""

    batch_size: int = 10  # 每批处理的记录数
    max_retries: int = 5  # 最大重试次数
    base_backoff_seconds: int = 60  # 基础退避秒数
    max_backoff_seconds: int = 3600  # 最大退避秒数（1小时）
    jitter_factor: float = 0.3  # 抖动因子 (0.0 ~ 1.0)
    loop_interval: float = 5.0  # loop 模式下每轮间隔（秒）
    lease_seconds: int = 120  # Lease 租约有效期（秒）

    # OpenMemory Client 配置（控制内部超时和重试）
    openmemory_timeout_seconds: float = 30.0  # OpenMemory HTTP 请求超时秒数
    openmemory_max_client_retries: int = (
        0  # OpenMemory 客户端内部最大重试次数（0=不重试，由 Worker 层控制重试）
    )


# ---------- 退避计算 ----------


def calculate_backoff_with_jitter(
    retry_count: int, base_seconds: int, max_seconds: int, jitter_factor: float
) -> int:
    """
    计算指数退避 + jitter

    公式：backoff = min(base * 2^retry, max) * (1 + random(-jitter, +jitter))

    Args:
        retry_count: 当前重试次数
        base_seconds: 基础退避秒数
        max_seconds: 最大退避秒数
        jitter_factor: 抖动因子

    Returns:
        退避秒数（整数）
    """
    # 指数退避
    backoff = base_seconds * (2**retry_count)
    # 限制上界
    backoff = min(backoff, max_seconds)
    # 添加 jitter
    jitter = random.uniform(-jitter_factor, jitter_factor)
    backoff = backoff * (1 + jitter)
    return max(1, int(backoff))


# ---------- 处理结果 ----------


@dataclass
class ProcessResult:
    """单条记录处理结果"""

    outbox_id: int
    success: bool
    action: str  # allow / redirect / reject
    reason: (
        str  # outbox_flush_success / outbox_flush_retry / outbox_flush_dead / outbox_flush_conflict
    )
    error: Optional[str] = None
    conflict: bool = False  # 是否发生冲突（lease 被抢占）


def _handle_db_error(
    outbox_id: int,
    worker_id: str,
    attempt_id: str,
    user_id: Optional[str],
    target_space: str,
    payload_sha: str,
    error: BaseException,
    correlation_id: Optional[str] = None,
) -> ProcessResult:
    """
    处理数据库错误：分类为 db_timeout 或 db_error，写入审计记录。

    数据库错误被视为可恢复错误，Worker 应进入重试路径。

    Args:
        outbox_id: Outbox 记录 ID
        worker_id: 当前 Worker ID
        attempt_id: 本次处理尝试的唯一标识
        user_id: 用户 ID（用于审计）
        target_space: 目标空间
        payload_sha: Payload SHA
        error: 捕获到的异常
        correlation_id: 批次级别的关联 ID

    Returns:
        ProcessResult 表示需要重试
    """
    error_type, reason = _classify_db_error(error)
    error_msg = f"{error_type}: {str(error)}"

    # 写入审计记录（action=redirect 表示需要重试）
    # 对于数据库错误，使用统一错误码
    try:
        event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id=correlation_id,
            actor_user_id=user_id,
            target_space=target_space,
            action="redirect",
            reason=reason,
            payload_sha=payload_sha,
            outbox_id=outbox_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
            extra={
                "error_type": error_type,
                "error_message": str(error),
            },
        )
        evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=event)
        logbook_adapter.insert_write_audit(
            actor_user_id=user_id,
            target_space=target_space,
            action="redirect",
            reason=reason,
            payload_sha=payload_sha,
            evidence_refs_json=evidence_refs_json,
        )
    except Exception as audit_err:
        # 审计写入失败也不应阻止重试流程
        logger.warning(f"[outbox:{outbox_id}] 写入 {error_type} 审计失败: {audit_err}")

    logger.warning(
        f"[outbox:{outbox_id}] 数据库错误 ({error_type}): {error}, "
        f"worker_id={worker_id}, attempt_id={attempt_id}"
    )

    return ProcessResult(
        outbox_id=outbox_id,
        success=False,
        action="redirect",
        reason=reason,
        error=error_msg,
        conflict=False,
    )


def _handle_conflict(
    outbox_id: int,
    worker_id: str,
    attempt_id: str,
    user_id: Optional[str],
    target_space: str,
    payload_sha: str,
    intended_action: str,
    correlation_id: Optional[str] = None,
) -> ProcessResult:
    """
    处理冲突情况：当 ack_sent/fail_retry/mark_dead 返回 False 时调用

    读取当前记录状态，写入 outbox_flush_conflict 审计（不重复写原有 action 的审计）

    Args:
        outbox_id: Outbox 记录 ID
        worker_id: 当前 Worker ID
        attempt_id: 本次处理尝试的唯一标识
        user_id: 用户 ID（用于审计）
        target_space: 目标空间
        payload_sha: Payload SHA
        intended_action: 原本计划执行的 action (success/retry/dead)
        correlation_id: 批次级别的关联 ID

    Returns:
        ProcessResult 表示冲突
    """
    # 读取当前记录状态
    current_record = logbook_adapter.get_outbox_by_id(outbox_id)

    observed_status = None
    observed_locked_by = None
    observed_last_error = None

    if current_record:
        observed_status = current_record.get("status")
        observed_locked_by = current_record.get("locked_by")
        observed_last_error = current_record.get("last_error")

    # 写入冲突审计（action=redirect 表示本次尝试被重定向/忽略）
    event = build_outbox_worker_audit_event(
        operation="outbox_flush",
        correlation_id=correlation_id,
        actor_user_id=user_id,
        target_space=target_space,
        action="redirect",
        reason=ErrorCode.OUTBOX_FLUSH_CONFLICT,
        payload_sha=payload_sha,
        outbox_id=outbox_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
        extra={
            "intended_action": intended_action,
            "observed_status": observed_status,
            "observed_locked_by": observed_locked_by,
            "observed_last_error": observed_last_error,
        },
    )
    evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=event)
    logbook_adapter.insert_write_audit(
        actor_user_id=user_id,
        target_space=target_space,
        action="redirect",
        reason=ErrorCode.OUTBOX_FLUSH_CONFLICT,
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
    )

    logger.warning(
        f"[outbox:{outbox_id}] 冲突检测: intended_action={intended_action}, "
        f"observed_status={observed_status}, observed_locked_by={observed_locked_by}, "
        f"worker_id={worker_id}, attempt_id={attempt_id}"
    )

    return ProcessResult(
        outbox_id=outbox_id,
        success=False,
        action="redirect",
        reason=ErrorCode.OUTBOX_FLUSH_CONFLICT,
        error=f"lease_conflict: observed_status={observed_status}, observed_locked_by={observed_locked_by}",
        conflict=True,
    )


# ---------- Worker 核心逻辑 ----------


def process_single_item(
    item: logbook_adapter.OutboxItem,
    worker_id: str,
    client: openmemory_client.OpenMemoryClient,
    config: WorkerConfig,
    attempt_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> ProcessResult:
    """
    处理单条 outbox 记录

    Args:
        item: Outbox 记录
        worker_id: Worker 标识符
        client: OpenMemory 客户端
        config: Worker 配置
        attempt_id: 本次处理尝试的唯一标识（用于冲突追踪）
        correlation_id: 批次级别的关联 ID（用于追踪同一批次的处理）

    Returns:
        ProcessResult 处理结果
    """
    outbox_id = item.outbox_id

    # 生成 attempt_id 用于冲突追踪
    if attempt_id is None:
        attempt_id = f"attempt-{uuid.uuid4().hex[:12]}"

    # 如果没有传入 correlation_id，使用 attempt_id 作为 correlation
    if correlation_id is None:
        correlation_id = f"corr-{uuid.uuid4().hex[:16]}"

    # 解析 target_space 获取 user_id（private:xxx -> xxx, team:xxx -> None）
    user_id = None
    if item.target_space.startswith("private:"):
        user_id = item.target_space[8:]  # len("private:") = 8

    # 捕获数据库错误（包括 statement_timeout）并归类为 db_timeout/db_error
    try:
        return _process_single_item_inner(
            item=item,
            worker_id=worker_id,
            client=client,
            config=config,
            attempt_id=attempt_id,
            correlation_id=correlation_id,
            user_id=user_id,
        )
    except psycopg.Error as e:
        # 数据库错误，分类并写入审计
        return _handle_db_error(
            outbox_id=outbox_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
            user_id=user_id,
            target_space=item.target_space,
            payload_sha=item.payload_sha,
            error=e,
            correlation_id=correlation_id,
        )


def _process_single_item_inner(
    item: logbook_adapter.OutboxItem,
    worker_id: str,
    client: openmemory_client.OpenMemoryClient,
    config: WorkerConfig,
    attempt_id: str,
    correlation_id: str,
    user_id: Optional[str],
) -> ProcessResult:
    """
    process_single_item 的内部实现，被外层 try-catch 包裹以捕获数据库错误。
    """
    outbox_id = item.outbox_id

    # Dedupe Check：处理前再次检查是否已成功写入（防止并发/重启重复）
    dedup_record = logbook_adapter.check_dedup(
        target_space=item.target_space,
        payload_sha=item.payload_sha,
    )
    if dedup_record:
        # 已存在成功写入的记录，直接标记当前记录为 sent 并返回
        logger.info(
            f"[outbox:{outbox_id}] Dedupe hit: 已存在成功写入记录 (original_outbox_id={dedup_record.get('outbox_id')})"
        )

        # 从原记录的 last_error 中提取 memory_id
        memory_id = None
        last_error = dedup_record.get("last_error")
        if last_error and last_error.startswith("memory_id="):
            memory_id = last_error.split("=", 1)[1]

        # 直接调用 ack_sent 标记当前记录为 sent
        ack_ok = logbook_adapter.ack_sent(
            outbox_id=outbox_id, worker_id=worker_id, memory_id=memory_id
        )

        # 检查返回值：若 False 表示发生冲突（lease 被抢占）
        if not ack_ok:
            return _handle_conflict(
                outbox_id=outbox_id,
                worker_id=worker_id,
                attempt_id=attempt_id,
                user_id=user_id,
                target_space=item.target_space,
                payload_sha=item.payload_sha,
                intended_action="dedup_hit",
                correlation_id=correlation_id,
            )

        # 写入审计（dedup_hit）
        event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id=correlation_id,
            actor_user_id=user_id,
            target_space=item.target_space,
            action="allow",
            reason=ErrorCode.OUTBOX_FLUSH_DEDUP_HIT,
            payload_sha=item.payload_sha,
            outbox_id=outbox_id,
            memory_id=memory_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
            extra={
                "original_outbox_id": dedup_record.get("outbox_id"),
            },
        )
        evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=event)
        logbook_adapter.insert_write_audit(
            actor_user_id=user_id,
            target_space=item.target_space,
            action="allow",
            reason=ErrorCode.OUTBOX_FLUSH_DEDUP_HIT,
            payload_sha=item.payload_sha,
            evidence_refs_json=evidence_refs_json,
        )

        return ProcessResult(
            outbox_id=outbox_id,
            success=True,
            action="allow",
            reason=ErrorCode.OUTBOX_FLUSH_DEDUP_HIT,
        )

    # 准备 metadata，包含 outbox_id/payload_sha/target_space 以便追踪
    metadata = {
        "source": "outbox_worker",
        "outbox_id": outbox_id,
        "payload_sha": item.payload_sha,
        "target_space": item.target_space,
    }
    if item.item_id:
        metadata["item_id"] = item.item_id

    # 在调用 OpenMemory 前续期 Lease，防止长时间调用导致租约过期
    logbook_adapter.renew_lease(outbox_id=outbox_id, worker_id=worker_id)

    # 调用 OpenMemory 写入，显式传 space=item.target_space
    # 捕获连接异常并转换为失败结果，确保重试逻辑正确执行
    try:
        result = client.store(
            content=item.payload_md, space=item.target_space, user_id=user_id, metadata=metadata
        )
    except openmemory_client.OpenMemoryConnectionError as e:
        logger.warning(f"[outbox:{outbox_id}] OpenMemory 连接失败: {e.message}")
        result = openmemory_client.StoreResult(
            success=False, error=f"connection_error: {e.message}"
        )
    except openmemory_client.OpenMemoryAPIError as e:
        logger.warning(f"[outbox:{outbox_id}] OpenMemory API 错误: {e.status_code} - {e.message}")
        result = openmemory_client.StoreResult(
            success=False, error=f"api_error: {e.status_code} - {e.message}"
        )
    except openmemory_client.OpenMemoryError as e:
        logger.warning(f"[outbox:{outbox_id}] OpenMemory 错误: {e.message}")
        result = openmemory_client.StoreResult(
            success=False, error=f"openmemory_error: {e.message}"
        )

    if result.success:
        # 成功后、ack 前再次续期，确保 ack 不会因租约过期失败
        logbook_adapter.renew_lease(outbox_id=outbox_id, worker_id=worker_id)

        # 成功：调用 ack_sent
        ack_ok = logbook_adapter.ack_sent(
            outbox_id=outbox_id, worker_id=worker_id, memory_id=result.memory_id
        )

        # 检查返回值：若 False 表示发生冲突（lease 被抢占）
        if not ack_ok:
            return _handle_conflict(
                outbox_id=outbox_id,
                worker_id=worker_id,
                attempt_id=attempt_id,
                user_id=user_id,
                target_space=item.target_space,
                payload_sha=item.payload_sha,
                intended_action="success",
                correlation_id=correlation_id,
            )

        # 写入审计
        event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id=correlation_id,
            actor_user_id=user_id,
            target_space=item.target_space,
            action="allow",
            reason=ErrorCode.OUTBOX_FLUSH_SUCCESS,
            payload_sha=item.payload_sha,
            outbox_id=outbox_id,
            memory_id=result.memory_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        )
        evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=event)
        logbook_adapter.insert_write_audit(
            actor_user_id=user_id,
            target_space=item.target_space,
            action="allow",
            reason=ErrorCode.OUTBOX_FLUSH_SUCCESS,
            payload_sha=item.payload_sha,
            evidence_refs_json=evidence_refs_json,
        )

        logger.info(f"[outbox:{outbox_id}] 写入成功, memory_id={result.memory_id}")
        return ProcessResult(
            outbox_id=outbox_id, success=True, action="allow", reason=ErrorCode.OUTBOX_FLUSH_SUCCESS
        )

    else:
        # 失败：检查是否超过最大重试次数
        new_retry_count = item.retry_count + 1
        error_msg = result.error or "unknown_error"

        if new_retry_count >= config.max_retries:
            # 超过最大重试：调用 mark_dead
            dead_ok = logbook_adapter.mark_dead(
                outbox_id=outbox_id, worker_id=worker_id, error=error_msg
            )

            # 检查返回值：若 False 表示发生冲突（lease 被抢占）
            if not dead_ok:
                return _handle_conflict(
                    outbox_id=outbox_id,
                    worker_id=worker_id,
                    attempt_id=attempt_id,
                    user_id=user_id,
                    target_space=item.target_space,
                    payload_sha=item.payload_sha,
                    intended_action="dead",
                    correlation_id=correlation_id,
                )

            # 写入审计
            event = build_outbox_worker_audit_event(
                operation="outbox_flush",
                correlation_id=correlation_id,
                actor_user_id=user_id,
                target_space=item.target_space,
                action="reject",
                reason=ErrorCode.OUTBOX_FLUSH_DEAD,
                payload_sha=item.payload_sha,
                outbox_id=outbox_id,
                retry_count=new_retry_count,
                worker_id=worker_id,
                attempt_id=attempt_id,
                extra={
                    "last_error": error_msg,
                },
            )
            evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=event)
            logbook_adapter.insert_write_audit(
                actor_user_id=user_id,
                target_space=item.target_space,
                action="reject",
                reason=ErrorCode.OUTBOX_FLUSH_DEAD,
                payload_sha=item.payload_sha,
                evidence_refs_json=evidence_refs_json,
            )

            logger.warning(
                f"[outbox:{outbox_id}] 超过最大重试次数({config.max_retries})，标记为 dead"
            )
            return ProcessResult(
                outbox_id=outbox_id,
                success=False,
                action="reject",
                reason=ErrorCode.OUTBOX_FLUSH_DEAD,
                error=error_msg,
            )

        else:
            # 未超过：调用 fail_retry
            # 使用 calculate_backoff_with_jitter 计算退避秒数
            backoff_seconds = calculate_backoff_with_jitter(
                retry_count=new_retry_count,
                base_seconds=config.base_backoff_seconds,
                max_seconds=config.max_backoff_seconds,
                jitter_factor=config.jitter_factor,
            )
            next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)

            retry_ok = logbook_adapter.fail_retry(
                outbox_id=outbox_id,
                worker_id=worker_id,
                error=error_msg,
                next_attempt_at=next_attempt_at,
            )

            # 检查返回值：若 False 表示发生冲突（lease 被抢占）
            if not retry_ok:
                return _handle_conflict(
                    outbox_id=outbox_id,
                    worker_id=worker_id,
                    attempt_id=attempt_id,
                    user_id=user_id,
                    target_space=item.target_space,
                    payload_sha=item.payload_sha,
                    intended_action="retry",
                    correlation_id=correlation_id,
                )

            # 写入审计（redirect 表示延后重试）
            event = build_outbox_worker_audit_event(
                operation="outbox_flush",
                correlation_id=correlation_id,
                actor_user_id=user_id,
                target_space=item.target_space,
                action="redirect",
                reason=ErrorCode.OUTBOX_FLUSH_RETRY,
                payload_sha=item.payload_sha,
                outbox_id=outbox_id,
                retry_count=new_retry_count,
                next_attempt_at=next_attempt_at.isoformat(),
                worker_id=worker_id,
                attempt_id=attempt_id,
                extra={
                    "last_error": error_msg,
                },
            )
            evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=event)
            logbook_adapter.insert_write_audit(
                actor_user_id=user_id,
                target_space=item.target_space,
                action="redirect",
                reason=ErrorCode.OUTBOX_FLUSH_RETRY,
                payload_sha=item.payload_sha,
                evidence_refs_json=evidence_refs_json,
            )

            logger.info(
                f"[outbox:{outbox_id}] 重试 {new_retry_count}/{config.max_retries}, 下次尝试: {next_attempt_at.isoformat()}"
            )
            return ProcessResult(
                outbox_id=outbox_id,
                success=False,
                action="redirect",
                reason=ErrorCode.OUTBOX_FLUSH_RETRY,
                error=error_msg,
            )


def process_batch(config: WorkerConfig, worker_id: Optional[str] = None) -> list[ProcessResult]:
    """
    处理一批 outbox 记录

    Args:
        config: Worker 配置
        worker_id: Worker 标识符（为 None 时自动生成）

    Returns:
        ProcessResult 列表
    """
    # 生成或使用 worker_id
    if worker_id is None:
        worker_id = f"worker-{uuid.uuid4().hex[:8]}"

    # 生成 correlation_id 用于追踪本批次处理
    correlation_id = f"corr-{uuid.uuid4().hex[:16]}"

    # 使用 claim_outbox 领取一批待处理记录（Lease 协议）
    raw_items = logbook_adapter.claim_outbox(
        worker_id=worker_id, limit=config.batch_size, lease_seconds=config.lease_seconds
    )

    if not raw_items:
        logger.debug("无待处理的 outbox 记录")
        return []

    # 转换为 OutboxItem 对象
    items = [logbook_adapter.OutboxItem.from_dict(row) for row in raw_items]

    logger.info(
        f"领取到 {len(items)} 条待处理记录 (worker_id={worker_id}, correlation_id={correlation_id})"
    )

    # 创建 OpenMemory 客户端，使用 WorkerConfig 中的超时和重试配置
    # 注意：设置 max_retries=0 或低值，由 Worker 层控制重试逻辑，避免内部重试导致处理时间失控
    client_retry_config = openmemory_client.RetryConfig(
        max_retries=config.openmemory_max_client_retries,
    )
    client = openmemory_client.OpenMemoryClient(
        timeout=config.openmemory_timeout_seconds,
        retry_config=client_retry_config,
    )

    # 依次处理每条记录
    results = []
    for item in items:
        # 为每条记录生成唯一 attempt_id
        attempt_id = f"attempt-{uuid.uuid4().hex[:12]}"
        try:
            result = process_single_item(
                item,
                worker_id,
                client,
                config,
                attempt_id=attempt_id,
                correlation_id=correlation_id,
            )
            results.append(result)
        except Exception as e:
            logger.error(f"[outbox:{item.outbox_id}] 处理异常: {e}")
            # 发生异常：调用 fail_retry
            try:
                # 计算退避时间
                new_retry_count = item.retry_count + 1
                backoff_seconds = calculate_backoff_with_jitter(
                    retry_count=new_retry_count,
                    base_seconds=config.base_backoff_seconds,
                    max_seconds=config.max_backoff_seconds,
                    jitter_factor=config.jitter_factor,
                )
                next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)

                retry_ok = logbook_adapter.fail_retry(
                    outbox_id=item.outbox_id,
                    worker_id=worker_id,
                    error=str(e),
                    next_attempt_at=next_attempt_at,
                )

                # 检查返回值：若 False 表示发生冲突
                if not retry_ok:
                    # 解析 user_id
                    user_id = None
                    if item.target_space.startswith("private:"):
                        user_id = item.target_space[8:]

                    conflict_result = _handle_conflict(
                        outbox_id=item.outbox_id,
                        worker_id=worker_id,
                        attempt_id=attempt_id,
                        user_id=user_id,
                        target_space=item.target_space,
                        payload_sha=item.payload_sha,
                        intended_action="exception_retry",
                        correlation_id=correlation_id,
                    )
                    results.append(conflict_result)
                    continue

            except Exception as retry_err:
                logger.error(f"[outbox:{item.outbox_id}] fail_retry 调用失败: {retry_err}")

            results.append(
                ProcessResult(
                    outbox_id=item.outbox_id,
                    success=False,
                    action="redirect",
                    reason=ErrorCode.OUTBOX_FLUSH_RETRY,
                    error=str(e),
                )
            )

    # 统计结果
    success_count = sum(1 for r in results if r.success)
    logger.info(f"批次处理完成: {success_count}/{len(results)} 成功")

    return results


def run_once(config: WorkerConfig, worker_id: Optional[str] = None) -> list[ProcessResult]:
    """
    执行一轮处理后退出

    Args:
        config: Worker 配置
        worker_id: Worker 标识符

    Returns:
        ProcessResult 列表
    """
    logger.info("Outbox Worker 启动 (--once 模式)")
    results = process_batch(config, worker_id)
    logger.info("Outbox Worker 完成")
    return results


def run_loop(config: WorkerConfig, worker_id: Optional[str] = None) -> None:
    """
    持续轮询模式

    Args:
        config: Worker 配置
        worker_id: Worker 标识符
    """
    # 生成固定的 worker_id 用于整个生命周期
    if worker_id is None:
        worker_id = f"worker-{uuid.uuid4().hex[:8]}"

    logger.info("Outbox Worker 启动 (--loop 模式)")
    logger.info(
        f"配置: batch_size={config.batch_size}, max_retries={config.max_retries}, "
        f"interval={config.loop_interval}s, lease={config.lease_seconds}s, worker_id={worker_id}"
    )

    try:
        while True:
            try:
                process_batch(config, worker_id)
            except Exception as e:
                logger.error(f"批次处理失败: {e}")

            time.sleep(config.loop_interval)

    except KeyboardInterrupt:
        logger.info("Outbox Worker 收到中断信号，退出")


# ---------- 命令行入口 ----------


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(description="Outbox Worker：处理 logbook.outbox_memory 队列")

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--once", action="store_true", help="执行一轮后退出")
    mode_group.add_argument("--loop", action="store_true", help="持续轮询模式")

    parser.add_argument("--batch-size", type=int, default=10, help="每批处理的记录数 (默认: 10)")
    parser.add_argument("--max-retries", type=int, default=5, help="最大重试次数 (默认: 5)")
    parser.add_argument("--base-backoff", type=int, default=60, help="基础退避秒数 (默认: 60)")
    parser.add_argument(
        "--loop-interval", type=float, default=5.0, help="loop 模式下每轮间隔秒数 (默认: 5.0)"
    )
    parser.add_argument(
        "--lease-seconds", type=int, default=120, help="Lease 租约有效期秒数 (默认: 120)"
    )
    parser.add_argument(
        "--openmemory-timeout",
        type=float,
        default=None,
        help="OpenMemory HTTP 请求超时秒数 (默认: 30.0, 可通过环境变量 OPENMEMORY_TIMEOUT_SECONDS 设置)",
    )
    parser.add_argument(
        "--openmemory-max-retries",
        type=int,
        default=None,
        help="OpenMemory 客户端内部最大重试次数 (默认: 0, 可通过环境变量 OPENMEMORY_MAX_CLIENT_RETRIES 设置)",
    )
    parser.add_argument(
        "--worker-id", type=str, default=None, help="Worker 标识符 (默认: 自动生成)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志输出")

    args = parser.parse_args()

    # 配置日志
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 解析 OpenMemory 配置（优先级：CLI 参数 > 环境变量 > 默认值）
    import os

    openmemory_timeout = args.openmemory_timeout
    if openmemory_timeout is None:
        env_timeout = os.getenv("OPENMEMORY_TIMEOUT_SECONDS")
        openmemory_timeout = float(env_timeout) if env_timeout else 30.0

    openmemory_max_retries = args.openmemory_max_retries
    if openmemory_max_retries is None:
        env_retries = os.getenv("OPENMEMORY_MAX_CLIENT_RETRIES")
        openmemory_max_retries = int(env_retries) if env_retries else 0

    # 构建配置
    config = WorkerConfig(
        batch_size=args.batch_size,
        max_retries=args.max_retries,
        base_backoff_seconds=args.base_backoff,
        loop_interval=args.loop_interval,
        lease_seconds=args.lease_seconds,
        openmemory_timeout_seconds=openmemory_timeout,
        openmemory_max_client_retries=openmemory_max_retries,
    )

    # 执行
    if args.once:
        results = run_once(config, args.worker_id)
        # 返回码：0=全部成功或无任务，1=有失败
        has_failure = any(not r.success for r in results)
        sys.exit(1 if has_failure else 0)
    else:
        run_loop(config, args.worker_id)


if __name__ == "__main__":
    main()
