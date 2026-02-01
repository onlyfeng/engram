#!/usr/bin/env python3
"""迭代证据文件命名与路径 helper。

本模块提供统一的证据文件命名规范，确保整个项目使用一致的文件名格式。

命名规范:
    1. Canonical path (固定文件名): iteration_{N}_evidence.json
       - 用于 record_iteration_evidence.py 输出
       - 用于文档引用（模板、回归记录）
       - 每个迭代只有一个权威证据文件

    2. Snapshot path (快照文件名): iteration_{N}_{timestamp}.json
       - 用于 promote_iteration.py 的 --create-evidence-stub
       - timestamp 格式: YYYYMMDD_HHMMSS
       - 可选包含 commit SHA 后缀

路径规范:
    - 证据目录: docs/acceptance/evidence/
    - 模板目录: docs/acceptance/_templates/
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 证据输出目录
EVIDENCE_DIR = REPO_ROOT / "docs" / "acceptance" / "evidence"

# 时间戳格式（用于快照文件名）
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

# ISO 8601 时间戳格式（用于 JSON 内部）
ISO_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# commit SHA pattern（7-40 位十六进制）
COMMIT_SHA_PATTERN = re.compile(r"^[a-f0-9]{7,40}$")


def canonical_evidence_filename(iteration_number: int) -> str:
    """生成 canonical 证据文件名（固定格式）。

    格式: iteration_{N}_evidence.json

    Args:
        iteration_number: 迭代编号（必须为正整数）

    Returns:
        文件名字符串

    Raises:
        ValueError: 如果迭代编号无效

    Examples:
        >>> canonical_evidence_filename(13)
        'iteration_13_evidence.json'
        >>> canonical_evidence_filename(1)
        'iteration_1_evidence.json'
    """
    _validate_iteration_number(iteration_number)
    return f"iteration_{iteration_number}_evidence.json"


def canonical_evidence_path(iteration_number: int) -> Path:
    """生成 canonical 证据文件完整路径。

    路径: docs/acceptance/evidence/iteration_{N}_evidence.json

    Args:
        iteration_number: 迭代编号（必须为正整数）

    Returns:
        完整路径 Path 对象

    Raises:
        ValueError: 如果迭代编号无效

    Examples:
        >>> str(canonical_evidence_path(13)).endswith('evidence/iteration_13_evidence.json')
        True
    """
    return EVIDENCE_DIR / canonical_evidence_filename(iteration_number)


def snapshot_evidence_filename(
    iteration_number: int,
    timestamp: Optional[datetime] = None,
    commit_sha: Optional[str] = None,
) -> str:
    """生成 snapshot 证据文件名（带时间戳）。

    格式:
        - 无 SHA: iteration_{N}_{timestamp}.json
        - 有 SHA: iteration_{N}_{timestamp}_{sha7}.json

    Args:
        iteration_number: 迭代编号（必须为正整数）
        timestamp: 时间戳（默认为当前时间）
        commit_sha: 可选的 commit SHA（将截取前 7 位）

    Returns:
        文件名字符串

    Raises:
        ValueError: 如果迭代编号或 commit SHA 无效

    Examples:
        >>> # 使用固定时间戳测试
        >>> from datetime import datetime
        >>> ts = datetime(2026, 2, 1, 10, 30, 0)
        >>> snapshot_evidence_filename(13, timestamp=ts)
        'iteration_13_20260201_103000.json'
        >>> snapshot_evidence_filename(13, timestamp=ts, commit_sha='abc1234def')
        'iteration_13_20260201_103000_abc1234.json'
    """
    _validate_iteration_number(iteration_number)

    if timestamp is None:
        timestamp = datetime.now()

    timestamp_str = timestamp.strftime(TIMESTAMP_FORMAT)

    if commit_sha is not None:
        _validate_commit_sha(commit_sha)
        sha_short = commit_sha[:7]
        return f"iteration_{iteration_number}_{timestamp_str}_{sha_short}.json"

    return f"iteration_{iteration_number}_{timestamp_str}.json"


def snapshot_evidence_path(
    iteration_number: int,
    timestamp: Optional[datetime] = None,
    commit_sha: Optional[str] = None,
) -> Path:
    """生成 snapshot 证据文件完整路径。

    路径: docs/acceptance/evidence/iteration_{N}_{timestamp}[_{sha7}].json

    Args:
        iteration_number: 迭代编号（必须为正整数）
        timestamp: 时间戳（默认为当前时间）
        commit_sha: 可选的 commit SHA（将截取前 7 位）

    Returns:
        完整路径 Path 对象

    Raises:
        ValueError: 如果迭代编号或 commit SHA 无效
    """
    return EVIDENCE_DIR / snapshot_evidence_filename(
        iteration_number, timestamp=timestamp, commit_sha=commit_sha
    )


def parse_evidence_filename(filename: str) -> dict[str, object]:
    """解析证据文件名，提取迭代编号、时间戳、SHA 等信息。

    支持的格式:
        - iteration_{N}_evidence.json (canonical)
        - iteration_{N}_{timestamp}.json (snapshot)
        - iteration_{N}_{timestamp}_{sha7}.json (snapshot with SHA)

    Args:
        filename: 文件名字符串

    Returns:
        包含解析结果的字典:
        - iteration_number: int
        - is_canonical: bool
        - timestamp: Optional[str] (YYYYMMDD_HHMMSS 格式)
        - commit_sha: Optional[str] (7 位短 SHA)

    Raises:
        ValueError: 如果文件名格式无效

    Examples:
        >>> parse_evidence_filename('iteration_13_evidence.json')
        {'iteration_number': 13, 'is_canonical': True, 'timestamp': None, 'commit_sha': None}
        >>> parse_evidence_filename('iteration_13_20260201_103000.json')
        {'iteration_number': 13, 'is_canonical': False, 'timestamp': '20260201_103000', 'commit_sha': None}
        >>> parse_evidence_filename('iteration_13_20260201_103000_abc1234.json')
        {'iteration_number': 13, 'is_canonical': False, 'timestamp': '20260201_103000', 'commit_sha': 'abc1234'}
    """
    # Canonical 格式: iteration_{N}_evidence.json
    canonical_pattern = re.compile(r"^iteration_(\d+)_evidence\.json$")
    match = canonical_pattern.match(filename)
    if match:
        return {
            "iteration_number": int(match.group(1)),
            "is_canonical": True,
            "timestamp": None,
            "commit_sha": None,
        }

    # Snapshot 格式（带 SHA）: iteration_{N}_{timestamp}_{sha7}.json
    snapshot_sha_pattern = re.compile(r"^iteration_(\d+)_(\d{8}_\d{6})_([a-f0-9]{7})\.json$")
    match = snapshot_sha_pattern.match(filename)
    if match:
        return {
            "iteration_number": int(match.group(1)),
            "is_canonical": False,
            "timestamp": match.group(2),
            "commit_sha": match.group(3),
        }

    # Snapshot 格式（无 SHA）: iteration_{N}_{timestamp}.json
    snapshot_pattern = re.compile(r"^iteration_(\d+)_(\d{8}_\d{6})\.json$")
    match = snapshot_pattern.match(filename)
    if match:
        return {
            "iteration_number": int(match.group(1)),
            "is_canonical": False,
            "timestamp": match.group(2),
            "commit_sha": None,
        }

    raise ValueError(f"无效的证据文件名格式: {filename}")


def relative_evidence_path(iteration_number: int, *, from_regression_doc: bool = True) -> str:
    """生成用于文档引用的相对路径。

    Args:
        iteration_number: 迭代编号
        from_regression_doc: 是否从 regression 文档引用（True 时路径为 evidence/...）

    Returns:
        相对路径字符串

    Examples:
        >>> relative_evidence_path(13)
        'evidence/iteration_13_evidence.json'
        >>> relative_evidence_path(13, from_regression_doc=False)
        'iteration_13_evidence.json'
    """
    filename = canonical_evidence_filename(iteration_number)
    if from_regression_doc:
        return f"evidence/{filename}"
    return filename


# ============================================================================
# 内部辅助函数
# ============================================================================


def _validate_iteration_number(iteration_number: int) -> None:
    """验证迭代编号有效性。

    Args:
        iteration_number: 迭代编号

    Raises:
        ValueError: 如果编号无效（非正整数）
    """
    if not isinstance(iteration_number, int) or iteration_number < 1:
        raise ValueError(f"迭代编号必须为正整数，实际值: {iteration_number}")


def _validate_commit_sha(commit_sha: str) -> None:
    """验证 commit SHA 有效性。

    Args:
        commit_sha: commit SHA 字符串

    Raises:
        ValueError: 如果 SHA 格式无效
    """
    if not COMMIT_SHA_PATTERN.match(commit_sha):
        raise ValueError(f"commit SHA 必须是 7-40 位十六进制字符串（小写），实际值: {commit_sha}")


# ============================================================================
# 常量导出（供其他模块使用）
# ============================================================================

__all__ = [
    "EVIDENCE_DIR",
    "TIMESTAMP_FORMAT",
    "ISO_TIMESTAMP_FORMAT",
    "canonical_evidence_filename",
    "canonical_evidence_path",
    "snapshot_evidence_filename",
    "snapshot_evidence_path",
    "parse_evidence_filename",
    "relative_evidence_path",
]
