#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artifact_bench.py - 制品存储轻量压测脚本

功能:
- 写入 N 个对象（包含大对象与小对象）
- 统计吞吐量与错误率
- 默认对 MinIO 运行

使用示例:
    # 默认压测（100 个对象，混合大小）
    python artifact_bench.py

    # 指定对象数量
    python artifact_bench.py --count 500

    # 指定最大对象大小（MB）
    python artifact_bench.py --max-size-mb 50

    # 4 线程并发
    python artifact_bench.py --concurrency 4

    # 指定前缀（自动添加时间戳隔离）
    python artifact_bench.py --prefix bench/

    # 压测后清理
    python artifact_bench.py --cleanup

    # JSON 输出
    python artifact_bench.py --json

环境变量:
    ENGRAM_S3_ENDPOINT     MinIO/S3 端点 URL
    ENGRAM_S3_ACCESS_KEY   访问密钥
    ENGRAM_S3_SECRET_KEY   密钥
    ENGRAM_S3_BUCKET       存储桶名称
"""

import argparse
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engram_step1.artifact_store import (
    ObjectStore,
    get_artifact_store,
    BACKEND_OBJECT,
)


# =============================================================================
# 常量
# =============================================================================

# 默认配置
DEFAULT_COUNT = 100                 # 默认写入对象数量
DEFAULT_MAX_SIZE_MB = 10            # 默认最大对象大小（MB）
DEFAULT_SMALL_RATIO = 0.7           # 小对象比例（70%）
DEFAULT_CONCURRENCY = 1             # 默认并发数
DEFAULT_PREFIX = "bench/"           # 默认前缀

# 大小阈值
SMALL_SIZE_MAX = 64 * 1024          # 小对象最大 64KB
LARGE_SIZE_MIN = 1 * 1024 * 1024    # 大对象最小 1MB


# =============================================================================
# 数据结构
# =============================================================================

@dataclass
class BenchItem:
    """单个压测项"""
    key: str                        # 对象 key
    size_bytes: int                 # 对象大小
    sha256: Optional[str] = None    # 内容 SHA256
    status: str = "pending"         # pending/success/failed/verified
    error: Optional[str] = None     # 错误信息
    write_duration_ms: float = 0    # 写入耗时（毫秒）
    read_duration_ms: float = 0     # 读取耗时（毫秒）


@dataclass
class BenchResult:
    """压测结果"""
    start_time: str = ""
    end_time: str = ""
    duration_seconds: float = 0
    
    # 对象统计
    total_count: int = 0
    small_count: int = 0
    large_count: int = 0
    
    # 写入统计
    write_success: int = 0
    write_failed: int = 0
    write_bytes: int = 0
    write_duration_ms: float = 0
    
    # 读取/校验统计
    read_success: int = 0
    read_failed: int = 0
    verify_success: int = 0
    verify_failed: int = 0
    
    # 吞吐量
    write_throughput_mbps: float = 0
    read_throughput_mbps: float = 0
    
    # 错误率
    write_error_rate: float = 0
    read_error_rate: float = 0
    
    # 详细信息
    errors: List[Dict[str, Any]] = field(default_factory=list)
    items: List[BenchItem] = field(default_factory=list)
    
    # 清理统计
    cleanup_count: int = 0
    cleanup_failed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": round(self.duration_seconds, 2),
            "total_count": self.total_count,
            "small_count": self.small_count,
            "large_count": self.large_count,
            "write_success": self.write_success,
            "write_failed": self.write_failed,
            "write_bytes": self.write_bytes,
            "write_duration_ms": round(self.write_duration_ms, 2),
            "write_throughput_mbps": round(self.write_throughput_mbps, 2),
            "write_error_rate": round(self.write_error_rate * 100, 2),
            "read_success": self.read_success,
            "read_failed": self.read_failed,
            "verify_success": self.verify_success,
            "verify_failed": self.verify_failed,
            "read_throughput_mbps": round(self.read_throughput_mbps, 2),
            "read_error_rate": round(self.read_error_rate * 100, 2),
            "cleanup_count": self.cleanup_count,
            "cleanup_failed": self.cleanup_failed,
            "errors": self.errors[:50],
        }


# =============================================================================
# 压测器
# =============================================================================

class ArtifactBench:
    """制品存储压测器"""

    def __init__(
        self,
        count: int = DEFAULT_COUNT,
        max_size_mb: int = DEFAULT_MAX_SIZE_MB,
        small_ratio: float = DEFAULT_SMALL_RATIO,
        prefix: str = DEFAULT_PREFIX,
        concurrency: int = DEFAULT_CONCURRENCY,
        verify: bool = False,
        cleanup: bool = False,
        verbose: bool = False,
    ):
        """
        初始化压测器

        Args:
            count: 写入对象数量
            max_size_mb: 最大对象大小（MB）
            small_ratio: 小对象比例
            prefix: 对象 key 前缀
            concurrency: 并发数
            verify: 是否校验写入结果
            cleanup: 是否清理压测对象
            verbose: 详细输出
        """
        self.count = count
        self.max_size_mb = max_size_mb
        self.small_ratio = small_ratio
        self.concurrency = max(1, concurrency)
        self.verify = verify
        self.cleanup = cleanup
        self.verbose = verbose

        # 添加时间戳隔离
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.prefix = f"{prefix.rstrip('/')}/{timestamp}/"

        # 存储实例
        self._store: Optional[ObjectStore] = None
        self._lock = threading.Lock()

    def _get_store(self) -> ObjectStore:
        """获取存储实例"""
        if self._store is None:
            store = get_artifact_store(backend=BACKEND_OBJECT)
            if not isinstance(store, ObjectStore):
                raise RuntimeError("压测需要 object 后端（S3/MinIO）")
            self._store = store
        return self._store

    def _generate_items(self) -> List[BenchItem]:
        """生成待写入的对象列表"""
        items = []
        small_count = int(self.count * self.small_ratio)
        large_count = self.count - small_count

        # 生成小对象
        for i in range(small_count):
            size = random.randint(1, SMALL_SIZE_MAX)
            key = f"{self.prefix}small/{i:06d}.bin"
            items.append(BenchItem(key=key, size_bytes=size))

        # 生成大对象
        max_size = self.max_size_mb * 1024 * 1024
        for i in range(large_count):
            size = random.randint(LARGE_SIZE_MIN, max_size)
            key = f"{self.prefix}large/{i:06d}.bin"
            items.append(BenchItem(key=key, size_bytes=size))

        # 随机打乱
        random.shuffle(items)
        return items

    def _generate_content(self, size: int) -> Tuple[bytes, str]:
        """
        生成指定大小的随机内容

        Args:
            size: 内容大小（字节）

        Returns:
            (content, sha256) 元组
        """
        # 使用可重复的随机数据（基于 size 作为种子）
        rng = random.Random(size)
        content = bytes(rng.getrandbits(8) for _ in range(size))
        sha256 = hashlib.sha256(content).hexdigest()
        return content, sha256

    def _write_item(self, item: BenchItem) -> BenchItem:
        """写入单个对象"""
        store = self._get_store()

        try:
            # 生成内容
            content, sha256 = self._generate_content(item.size_bytes)
            item.sha256 = sha256

            # 写入
            start_time = time.monotonic()
            result = store.put(item.key, content)
            item.write_duration_ms = (time.monotonic() - start_time) * 1000

            # 验证返回的 sha256
            if result.get("sha256") != sha256:
                item.status = "failed"
                item.error = f"SHA256 不匹配: expected={sha256}, got={result.get('sha256')}"
            else:
                item.status = "success"

        except Exception as e:
            item.status = "failed"
            item.error = str(e)

        return item

    def _verify_item(self, item: BenchItem) -> BenchItem:
        """校验单个对象"""
        if item.status != "success":
            return item

        store = self._get_store()

        try:
            start_time = time.monotonic()
            info = store.get_info(item.key)
            item.read_duration_ms = (time.monotonic() - start_time) * 1000

            if info.get("sha256") != item.sha256:
                item.status = "verify_failed"
                item.error = f"校验失败: expected={item.sha256}, got={info.get('sha256')}"
            else:
                item.status = "verified"

        except Exception as e:
            item.status = "verify_failed"
            item.error = f"校验异常: {e}"

        return item

    def _cleanup_items(self, items: List[BenchItem]) -> Tuple[int, int]:
        """
        清理压测对象

        Returns:
            (成功数, 失败数)
        """
        store = self._get_store()
        client = store._get_client()
        bucket = store.bucket

        success = 0
        failed = 0

        for item in items:
            try:
                key = store._object_key(item.key)
                client.delete_object(Bucket=bucket, Key=key)
                success += 1
            except Exception as e:
                failed += 1
                if self.verbose:
                    print(f"清理失败 {item.key}: {e}", file=sys.stderr)

        return success, failed

    def run(self) -> BenchResult:
        """执行压测"""
        result = BenchResult()
        result.start_time = datetime.now().isoformat()

        # 验证存储配置
        try:
            store = self._get_store()
            if self.verbose:
                print(f"[BENCH] 存储后端: {type(store).__name__}")
                print(f"[BENCH] Bucket: {store.bucket}")
                print(f"[BENCH] Prefix: {self.prefix}")
        except Exception as e:
            result.end_time = datetime.now().isoformat()
            result.errors.append({"error": f"存储配置失败: {e}"})
            return result

        # 生成待写入对象
        items = self._generate_items()
        result.total_count = len(items)
        result.small_count = sum(1 for i in items if "small" in i.key)
        result.large_count = sum(1 for i in items if "large" in i.key)

        if self.verbose:
            print(f"[BENCH] 待写入对象: {result.total_count} 个")
            print(f"[BENCH]   - 小对象 (<=64KB): {result.small_count} 个")
            print(f"[BENCH]   - 大对象 (>=1MB): {result.large_count} 个")
            total_bytes = sum(i.size_bytes for i in items)
            print(f"[BENCH]   - 总大小: {total_bytes / 1024 / 1024:.2f} MB")

        # 写入阶段
        if self.verbose:
            print(f"\n[BENCH] 开始写入（并发: {self.concurrency}）...")

        write_start = time.monotonic()

        if self.concurrency > 1:
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {executor.submit(self._write_item, item): item for item in items}
                for future in as_completed(futures):
                    try:
                        written_item = future.result()
                        if written_item.status == "success":
                            result.write_success += 1
                            result.write_bytes += written_item.size_bytes
                            result.write_duration_ms += written_item.write_duration_ms
                        else:
                            result.write_failed += 1
                            result.errors.append({
                                "key": written_item.key,
                                "phase": "write",
                                "error": written_item.error,
                            })
                    except Exception as e:
                        result.write_failed += 1
                        result.errors.append({"phase": "write", "error": str(e)})
        else:
            for item in items:
                written_item = self._write_item(item)
                if written_item.status == "success":
                    result.write_success += 1
                    result.write_bytes += written_item.size_bytes
                    result.write_duration_ms += written_item.write_duration_ms
                else:
                    result.write_failed += 1
                    result.errors.append({
                        "key": written_item.key,
                        "phase": "write",
                        "error": written_item.error,
                    })

                if self.verbose and (result.write_success + result.write_failed) % 50 == 0:
                    print(f"[BENCH]   进度: {result.write_success + result.write_failed}/{result.total_count}")

        write_end = time.monotonic()
        write_wall_time = write_end - write_start

        # 计算写入吞吐量
        if write_wall_time > 0:
            result.write_throughput_mbps = result.write_bytes / 1024 / 1024 / write_wall_time

        # 计算写入错误率
        if result.total_count > 0:
            result.write_error_rate = result.write_failed / result.total_count

        if self.verbose:
            print(f"[BENCH] 写入完成:")
            print(f"[BENCH]   - 成功: {result.write_success}")
            print(f"[BENCH]   - 失败: {result.write_failed}")
            print(f"[BENCH]   - 吞吐量: {result.write_throughput_mbps:.2f} MB/s")
            print(f"[BENCH]   - 错误率: {result.write_error_rate * 100:.2f}%")

        # 校验阶段
        if self.verify:
            if self.verbose:
                print(f"\n[BENCH] 开始校验...")

            read_start = time.monotonic()
            read_bytes = 0

            if self.concurrency > 1:
                with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                    futures = {executor.submit(self._verify_item, item): item for item in items}
                    for future in as_completed(futures):
                        try:
                            verified_item = future.result()
                            if verified_item.status == "verified":
                                result.read_success += 1
                                result.verify_success += 1
                                read_bytes += verified_item.size_bytes
                            elif verified_item.status == "verify_failed":
                                result.read_failed += 1
                                result.verify_failed += 1
                                result.errors.append({
                                    "key": verified_item.key,
                                    "phase": "verify",
                                    "error": verified_item.error,
                                })
                        except Exception as e:
                            result.read_failed += 1
                            result.errors.append({"phase": "verify", "error": str(e)})
            else:
                for item in items:
                    verified_item = self._verify_item(item)
                    if verified_item.status == "verified":
                        result.read_success += 1
                        result.verify_success += 1
                        read_bytes += verified_item.size_bytes
                    elif verified_item.status == "verify_failed":
                        result.read_failed += 1
                        result.verify_failed += 1
                        result.errors.append({
                            "key": verified_item.key,
                            "phase": "verify",
                            "error": verified_item.error,
                        })

            read_end = time.monotonic()
            read_wall_time = read_end - read_start

            # 计算读取吞吐量
            if read_wall_time > 0:
                result.read_throughput_mbps = read_bytes / 1024 / 1024 / read_wall_time

            # 计算读取错误率
            if result.write_success > 0:
                result.read_error_rate = result.read_failed / result.write_success

            if self.verbose:
                print(f"[BENCH] 校验完成:")
                print(f"[BENCH]   - 成功: {result.verify_success}")
                print(f"[BENCH]   - 失败: {result.verify_failed}")
                print(f"[BENCH]   - 吞吐量: {result.read_throughput_mbps:.2f} MB/s")

        # 清理阶段
        if self.cleanup:
            if self.verbose:
                print(f"\n[BENCH] 开始清理...")

            result.cleanup_count, result.cleanup_failed = self._cleanup_items(items)

            if self.verbose:
                print(f"[BENCH] 清理完成:")
                print(f"[BENCH]   - 成功: {result.cleanup_count}")
                print(f"[BENCH]   - 失败: {result.cleanup_failed}")

        result.end_time = datetime.now().isoformat()
        result.duration_seconds = time.monotonic() - (write_start - (write_end - write_start))
        result.items = items

        return result


# =============================================================================
# CLI 入口
# =============================================================================

def main() -> int:
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="制品存储轻量压测脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 默认压测（100 个对象，混合大小）
  python artifact_bench.py

  # 指定对象数量
  python artifact_bench.py --count 500

  # 指定最大对象大小（MB）
  python artifact_bench.py --max-size-mb 50

  # 4 线程并发
  python artifact_bench.py --concurrency 4

  # 压测并校验
  python artifact_bench.py --verify

  # 压测后清理
  python artifact_bench.py --cleanup

  # JSON 输出
  python artifact_bench.py --json

环境变量:
  ENGRAM_S3_ENDPOINT     MinIO/S3 端点 URL（如 http://localhost:9000）
  ENGRAM_S3_ACCESS_KEY   访问密钥
  ENGRAM_S3_SECRET_KEY   密钥
  ENGRAM_S3_BUCKET       存储桶名称
        """
    )

    parser.add_argument(
        "--count", "-n",
        type=int,
        default=DEFAULT_COUNT,
        help=f"写入对象数量（默认: {DEFAULT_COUNT}）"
    )

    parser.add_argument(
        "--max-size-mb",
        type=int,
        default=DEFAULT_MAX_SIZE_MB,
        help=f"最大对象大小（MB，默认: {DEFAULT_MAX_SIZE_MB}）"
    )

    parser.add_argument(
        "--small-ratio",
        type=float,
        default=DEFAULT_SMALL_RATIO,
        help=f"小对象比例（0.0-1.0，默认: {DEFAULT_SMALL_RATIO}）"
    )

    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"对象 key 前缀（默认: {DEFAULT_PREFIX}）"
    )

    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"并发线程数（默认: {DEFAULT_CONCURRENCY}）"
    )

    parser.add_argument(
        "--verify",
        action="store_true",
        help="写入后校验对象完整性"
    )

    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="压测后清理写入的对象"
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出"
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果"
    )

    args = parser.parse_args()

    # 验证参数
    if args.count < 1:
        print(f"错误: --count 必须 >= 1", file=sys.stderr)
        return 2

    if args.max_size_mb < 1:
        print(f"错误: --max-size-mb 必须 >= 1", file=sys.stderr)
        return 2

    if not 0.0 <= args.small_ratio <= 1.0:
        print(f"错误: --small-ratio 必须在 0.0 到 1.0 之间", file=sys.stderr)
        return 2

    if args.concurrency < 1:
        print(f"错误: --concurrency 必须 >= 1", file=sys.stderr)
        return 2

    # 检查环境变量
    required_envs = ["ENGRAM_S3_ENDPOINT", "ENGRAM_S3_ACCESS_KEY", "ENGRAM_S3_SECRET_KEY", "ENGRAM_S3_BUCKET"]
    missing_envs = [e for e in required_envs if not os.environ.get(e)]
    if missing_envs:
        print(f"错误: 缺少必需的环境变量: {', '.join(missing_envs)}", file=sys.stderr)
        print("\n请设置以下环境变量:", file=sys.stderr)
        print("  export ENGRAM_S3_ENDPOINT=http://localhost:9000", file=sys.stderr)
        print("  export ENGRAM_S3_ACCESS_KEY=minioadmin", file=sys.stderr)
        print("  export ENGRAM_S3_SECRET_KEY=minioadmin", file=sys.stderr)
        print("  export ENGRAM_S3_BUCKET=engram", file=sys.stderr)
        return 2

    try:
        bench = ArtifactBench(
            count=args.count,
            max_size_mb=args.max_size_mb,
            small_ratio=args.small_ratio,
            prefix=args.prefix,
            concurrency=args.concurrency,
            verify=args.verify,
            cleanup=args.cleanup,
            verbose=args.verbose or not args.json,
        )

        result = bench.run()

        if args.json:
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        else:
            # 打印摘要
            print("\n" + "=" * 60)
            print("压测结果摘要")
            print("=" * 60)
            print(f"  开始时间:     {result.start_time}")
            print(f"  结束时间:     {result.end_time}")
            print(f"  总耗时:       {result.duration_seconds:.2f} 秒")
            print("-" * 60)
            print(f"  总对象数:     {result.total_count}")
            print(f"    - 小对象:   {result.small_count}")
            print(f"    - 大对象:   {result.large_count}")
            print("-" * 60)
            print("写入统计:")
            print(f"    成功:       {result.write_success}")
            print(f"    失败:       {result.write_failed}")
            print(f"    总字节:     {result.write_bytes / 1024 / 1024:.2f} MB")
            print(f"    吞吐量:     {result.write_throughput_mbps:.2f} MB/s")
            print(f"    错误率:     {result.write_error_rate * 100:.2f}%")

            if args.verify:
                print("-" * 60)
                print("校验统计:")
                print(f"    成功:       {result.verify_success}")
                print(f"    失败:       {result.verify_failed}")
                print(f"    吞吐量:     {result.read_throughput_mbps:.2f} MB/s")
                print(f"    错误率:     {result.read_error_rate * 100:.2f}%")

            if args.cleanup:
                print("-" * 60)
                print("清理统计:")
                print(f"    成功:       {result.cleanup_count}")
                print(f"    失败:       {result.cleanup_failed}")

            if result.errors:
                print("-" * 60)
                print("错误列表:")
                for err in result.errors[:10]:
                    key = err.get("key", "unknown")
                    phase = err.get("phase", "unknown")
                    error = err.get("error", "unknown")
                    print(f"    [{phase}] {key}: {error}")
                if len(result.errors) > 10:
                    print(f"    ... 还有 {len(result.errors) - 10} 个错误")

            print("=" * 60)

        # 返回码
        if result.write_failed > 0 or result.verify_failed > 0:
            return 1
        return 0

    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
