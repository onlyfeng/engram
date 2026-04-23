from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from engram.logbook.errors import ErrorCode

from .openmemory_client import (
    GetResult,
    RetryConfig,
    extract_memory_object_content,
    extract_memory_object_payload_sha,
    extract_memory_object_space,
)
from .services.hash_utils import compute_payload_sha

READBACK_VERIFY_ATTEMPTS = 3
READBACK_VERIFY_DELAY_SECONDS = 0.1
READBACK_VERIFY_GET_RETRY_CONFIG = RetryConfig(
    max_retries=0,
    base_delay=READBACK_VERIFY_DELAY_SECONDS,
    max_delay=READBACK_VERIFY_DELAY_SECONDS,
    jitter=0.0,
)


@dataclass
class ReadbackValidationFailure:
    reason: str
    message: str


@dataclass
class ReadbackVerificationResult:
    failure: Optional[ReadbackValidationFailure] = None
    skipped_error: Optional[str] = None


@runtime_checkable
class ReadbackClient(Protocol):
    def get_memory(
        self,
        memory_id: str,
        retry_config: Optional[RetryConfig] = None,
        user_id: Optional[str] = None,
    ) -> GetResult: ...


def classify_readback_fetch_failure(error: Optional[str]) -> Optional[ReadbackValidationFailure]:
    """仅将可确定的一致性异常收敛为稳定 reason。"""
    if error == "memory_not_found":
        return ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_NOT_FOUND,
            message="memory_get 未找到刚写入的对象",
        )
    if error and error.startswith("memory_id_mismatch:"):
        return ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_MEMORY_ID_MISMATCH,
            message=f"memory_get 返回了错误对象: {error}",
        )
    if error and error.startswith("invalid_memory_payload:"):
        return ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_INVALID_PAYLOAD,
            message=f"memory_get 返回了无效对象: {error}",
        )
    return None


def validate_readback_memory(
    *,
    memory: Any,
    expected_space: str,
    expected_payload_sha: str,
) -> Optional[ReadbackValidationFailure]:
    """校验写后读对象的关键一致性字段。"""
    actual_space = extract_memory_object_space(memory)
    if actual_space != expected_space:
        return ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_SPACE_MISMATCH,
            message=f"memory_get 返回的 space 不一致: expected={expected_space}, actual={actual_space}",
        )

    content = extract_memory_object_content(memory)
    if not isinstance(content, str) or not content.strip():
        return ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_EMPTY_CONTENT,
            message="memory_get 返回的 content 为空",
        )

    actual_payload_sha = extract_memory_object_payload_sha(memory)
    if actual_payload_sha is None:
        actual_payload_sha = compute_payload_sha(content)
    if actual_payload_sha != expected_payload_sha:
        return ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_PAYLOAD_MISMATCH,
            message=(
                "memory_get 返回的 payload_sha 不一致: "
                f"expected={expected_payload_sha}, actual={actual_payload_sha}"
            ),
        )

    return None


def supports_readback_retry_config(client: object) -> bool:
    """判断 client.get_memory 是否显式支持 retry_config 或 **kwargs。"""
    get_memory = getattr(client, "get_memory", None)
    if not callable(get_memory):
        return False

    try:
        signature = inspect.signature(get_memory)
    except (TypeError, ValueError):
        return False

    parameters = signature.parameters.values()
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == "retry_config"
        for parameter in parameters
    )


def supports_readback_user_id(client: object) -> bool:
    """判断 client.get_memory 是否显式支持 user_id 或 **kwargs。"""
    get_memory = getattr(client, "get_memory", None)
    if not callable(get_memory):
        return False

    try:
        signature = inspect.signature(get_memory)
    except (TypeError, ValueError):
        return False

    parameters = signature.parameters.values()
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == "user_id"
        for parameter in parameters
    )


def _evaluate_readback_attempt(
    *,
    get_result: object,
    expected_space: str,
    expected_payload_sha: str,
) -> tuple[Optional[ReadbackValidationFailure], Optional[str], bool]:
    if not isinstance(get_result, GetResult):
        return None, None, True

    if not get_result.success:
        error = get_result.error
        failure = classify_readback_fetch_failure(error)
        if failure is not None:
            return failure, None, False
        return None, error or "unknown_error", False

    failure = validate_readback_memory(
        memory=get_result.memory,
        expected_space=expected_space,
        expected_payload_sha=expected_payload_sha,
    )
    return failure, None, False


async def verify_readback_after_store_async(
    *,
    client: object,
    memory_id: str,
    expected_space: str,
    expected_payload_sha: str,
    openmemory_user_id: Optional[str] = None,
) -> ReadbackVerificationResult:
    """异步写后读校验，供 request handler 使用。"""
    if not isinstance(client, ReadbackClient) or not supports_readback_retry_config(client):
        return ReadbackVerificationResult()

    pass_user_id = supports_readback_user_id(client)
    last_failure: Optional[ReadbackValidationFailure] = None
    last_skipped_error: Optional[str] = None

    for attempt in range(READBACK_VERIFY_ATTEMPTS):
        extra_kwargs: Dict[str, Any] = {"user_id": openmemory_user_id} if pass_user_id else {}
        get_result = client.get_memory(
            memory_id,
            retry_config=READBACK_VERIFY_GET_RETRY_CONFIG,
            **extra_kwargs,
        )
        failure, skipped_error, should_skip = _evaluate_readback_attempt(
            get_result=get_result,
            expected_space=expected_space,
            expected_payload_sha=expected_payload_sha,
        )
        if should_skip:
            return ReadbackVerificationResult()

        if failure is None and skipped_error is None:
            return ReadbackVerificationResult()

        if failure is not None:
            last_failure = failure
            last_skipped_error = None
        elif skipped_error is not None:
            # Transient error clears only NOT_FOUND (propagation-delay uncertainty, per
            # original memory_store.py design). Concrete integrity violations such as
            # space_mismatch or payload_mismatch cannot be caused by propagation delay
            # and must not be overwritten by a later network blip.
            not_found_only = (
                last_failure is None
                or last_failure.reason == ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_NOT_FOUND
            )
            if not_found_only:
                last_failure = None
                last_skipped_error = skipped_error

        if attempt < READBACK_VERIFY_ATTEMPTS - 1:
            await asyncio.sleep(READBACK_VERIFY_DELAY_SECONDS)

    return ReadbackVerificationResult(
        failure=last_failure,
        skipped_error=last_skipped_error,
    )


def verify_readback_after_store(
    *,
    client: object,
    memory_id: str,
    expected_space: str,
    expected_payload_sha: str,
    openmemory_user_id: Optional[str] = None,
) -> ReadbackVerificationResult:
    """同步写后读校验，供 outbox worker 使用。"""
    if not isinstance(client, ReadbackClient) or not supports_readback_retry_config(client):
        return ReadbackVerificationResult()

    pass_user_id = supports_readback_user_id(client)
    last_failure: Optional[ReadbackValidationFailure] = None
    last_skipped_error: Optional[str] = None

    for attempt in range(READBACK_VERIFY_ATTEMPTS):
        extra_kwargs: Dict[str, Any] = {"user_id": openmemory_user_id} if pass_user_id else {}
        get_result = client.get_memory(
            memory_id,
            retry_config=READBACK_VERIFY_GET_RETRY_CONFIG,
            **extra_kwargs,
        )
        failure, skipped_error, should_skip = _evaluate_readback_attempt(
            get_result=get_result,
            expected_space=expected_space,
            expected_payload_sha=expected_payload_sha,
        )
        if should_skip:
            return ReadbackVerificationResult()

        if failure is None and skipped_error is None:
            return ReadbackVerificationResult()

        if failure is not None:
            last_failure = failure
            last_skipped_error = None
        elif skipped_error is not None:
            not_found_only = (
                last_failure is None
                or last_failure.reason == ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_NOT_FOUND
            )
            if not_found_only:
                last_failure = None
                last_skipped_error = skipped_error

        if attempt < READBACK_VERIFY_ATTEMPTS - 1:
            time.sleep(READBACK_VERIFY_DELAY_SECONDS)

    return ReadbackVerificationResult(
        failure=last_failure,
        skipped_error=last_skipped_error,
    )
