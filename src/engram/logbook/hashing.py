"""
engram_logbook.hashing - 哈希计算工具模块

提供文件和内容的哈希计算功能。
"""

import hashlib
from pathlib import Path
from typing import BinaryIO, Optional, Union

from .errors import FileHashNotFoundError, HashingError

# 默认哈希算法
DEFAULT_ALGORITHM = "sha256"

# 文件读取缓冲区大小（64KB）
BUFFER_SIZE = 65536


def hash_bytes(data: bytes, algorithm: str = DEFAULT_ALGORITHM) -> str:
    """
    计算字节数据的哈希值

    Args:
        data: 字节数据
        algorithm: 哈希算法（sha256, sha1, md5 等）

    Returns:
        十六进制哈希字符串
    """
    try:
        hasher = hashlib.new(algorithm)
        hasher.update(data)
        return hasher.hexdigest()
    except ValueError as e:
        raise HashingError(
            f"不支持的哈希算法: {algorithm}",
            {"algorithm": algorithm, "error": str(e)},
        )


def hash_string(text: str, algorithm: str = DEFAULT_ALGORITHM, encoding: str = "utf-8") -> str:
    """
    计算字符串的哈希值

    Args:
        text: 字符串
        algorithm: 哈希算法
        encoding: 字符串编码

    Returns:
        十六进制哈希字符串
    """
    return hash_bytes(text.encode(encoding), algorithm)


def hash_file(
    file_path: Union[str, Path],
    algorithm: str = DEFAULT_ALGORITHM,
    buffer_size: int = BUFFER_SIZE,
) -> str:
    """
    计算文件的哈希值

    Args:
        file_path: 文件路径
        algorithm: 哈希算法
        buffer_size: 读取缓冲区大小

    Returns:
        十六进制哈希字符串

    Raises:
        FileNotFoundError: 文件不存在时
        HashingError: 计算失败时
    """
    path = Path(file_path)

    if not path.exists():
        raise FileHashNotFoundError(
            f"文件不存在: {file_path}",
            {"path": str(path.absolute())},
        )

    if not path.is_file():
        raise HashingError(
            f"路径不是文件: {file_path}",
            {"path": str(path.absolute())},
        )

    try:
        hasher = hashlib.new(algorithm)
        with open(path, "rb") as f:
            _hash_stream(f, hasher, buffer_size)
        return hasher.hexdigest()
    except (OSError, IOError) as e:
        raise HashingError(
            f"读取文件失败: {e}",
            {"path": str(path.absolute()), "error": str(e)},
        )
    except ValueError as e:
        raise HashingError(
            f"不支持的哈希算法: {algorithm}",
            {"algorithm": algorithm, "error": str(e)},
        )


def hash_stream(
    stream: BinaryIO,
    algorithm: str = DEFAULT_ALGORITHM,
    buffer_size: int = BUFFER_SIZE,
) -> str:
    """
    计算二进制流的哈希值

    Args:
        stream: 二进制流对象
        algorithm: 哈希算法
        buffer_size: 读取缓冲区大小

    Returns:
        十六进制哈希字符串
    """
    try:
        hasher = hashlib.new(algorithm)
        _hash_stream(stream, hasher, buffer_size)
        return hasher.hexdigest()
    except ValueError as e:
        raise HashingError(
            f"不支持的哈希算法: {algorithm}",
            {"algorithm": algorithm, "error": str(e)},
        )


def _hash_stream(stream: BinaryIO, hasher, buffer_size: int) -> None:
    """内部函数：对流进行哈希计算"""
    while True:
        data = stream.read(buffer_size)
        if not data:
            break
        hasher.update(data)


def verify_file_hash(
    file_path: Union[str, Path],
    expected_hash: str,
    algorithm: str = DEFAULT_ALGORITHM,
) -> bool:
    """
    验证文件哈希值

    Args:
        file_path: 文件路径
        expected_hash: 预期的哈希值
        algorithm: 哈希算法

    Returns:
        哈希是否匹配
    """
    actual_hash = hash_file(file_path, algorithm)
    return actual_hash.lower() == expected_hash.lower()


def get_file_info(
    file_path: Union[str, Path],
    algorithm: str = DEFAULT_ALGORITHM,
) -> dict:
    """
    获取文件信息（包含哈希值）

    Args:
        file_path: 文件路径
        algorithm: 哈希算法

    Returns:
        包含文件信息的字典
    """
    path = Path(file_path)

    if not path.exists():
        raise FileHashNotFoundError(
            f"文件不存在: {file_path}",
            {"path": str(path.absolute())},
        )

    stat = path.stat()
    return {
        "path": str(path.absolute()),
        "name": path.name,
        "size": stat.st_size,
        "hash": hash_file(path, algorithm),
        "algorithm": algorithm,
    }


# 预定义的常用哈希函数
def sha256(data: Union[bytes, str]) -> str:
    """计算 SHA-256 哈希"""
    if isinstance(data, str):
        return hash_string(data, "sha256")
    return hash_bytes(data, "sha256")


def sha1(data: Union[bytes, str]) -> str:
    """计算 SHA-1 哈希"""
    if isinstance(data, str):
        return hash_string(data, "sha1")
    return hash_bytes(data, "sha1")


def md5(data: Union[bytes, str]) -> str:
    """计算 MD5 哈希"""
    if isinstance(data, str):
        return hash_string(data, "md5")
    return hash_bytes(data, "md5")
