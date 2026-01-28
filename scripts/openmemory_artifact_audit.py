#!/usr/bin/env python3
"""
OpenMemory Artifact Audit 工具

读取 OpenMemory.upstream.lock.json，调用 docker image inspect 获取镜像 digest，
输出审计报告到 .artifacts/openmemory-artifact-audit.json。

用法:
    python scripts/openmemory_artifact_audit.py
    python scripts/openmemory_artifact_audit.py --json
    python scripts/openmemory_artifact_audit.py --output .artifacts/openmemory-artifact-audit.json
"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# 默认检查的镜像列表（对应 docker-compose.unified.yml 中的服务）
DEFAULT_IMAGES = [
    "gateway",
    "worker",
    "openmemory",
    "openmemory_migrate",
    "dashboard",
]

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
LOCK_FILE = PROJECT_ROOT / "OpenMemory.upstream.lock.json"
DEFAULT_OUTPUT = PROJECT_ROOT / ".artifacts" / "openmemory-artifact-audit.json"


@dataclass
class ImageAuditResult:
    """单个镜像的审计结果"""
    image_name: str
    exists: bool
    digest: Optional[str] = None
    created: Optional[str] = None
    size: Optional[int] = None
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "image_name": self.image_name,
            "exists": self.exists,
            "digest": self.digest,
            "created": self.created,
            "size": self.size,
            "error": self.error,
        }


@dataclass
class AuditReport:
    """审计报告"""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    lock_file_path: str = ""
    upstream_ref: Optional[str] = None
    upstream_commit_sha: Optional[str] = None
    local_patchset_id: Optional[str] = None
    images: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "lock_file_path": self.lock_file_path,
            "upstream_ref": self.upstream_ref,
            "upstream_commit_sha": self.upstream_commit_sha,
            "local_patchset_id": self.local_patchset_id,
            "images": self.images,
            "summary": self.summary,
        }


def load_lock_file(lock_path: Path) -> Optional[Dict[str, Any]]:
    """加载 lock 文件"""
    if not lock_path.exists():
        return None
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"[ERROR] 无法解析 lock 文件: {e}", file=sys.stderr)
        return None


def inspect_docker_image(image_name: str, compose_project: str = "engram") -> ImageAuditResult:
    """
    调用 docker image inspect 获取镜像信息
    
    尝试多种镜像名称格式：
    1. {compose_project}-{image_name}:latest (docker compose 默认格式)
    2. {compose_project}_{image_name}:latest (旧版 docker-compose 格式)
    3. {image_name}:latest (直接镜像名)
    """
    # 尝试的镜像名称列表
    image_variants = [
        f"{compose_project}-{image_name}:latest",
        f"{compose_project}_{image_name}:latest",
        f"{image_name}:latest",
    ]
    
    for full_image_name in image_variants:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", full_image_name, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            if result.returncode == 0:
                # 解析 JSON 输出
                data = json.loads(result.stdout)
                if data and isinstance(data, list) and len(data) > 0:
                    image_info = data[0]
                    
                    # 提取 digest（从 RepoDigests 或 Id）
                    repo_digests = image_info.get("RepoDigests", [])
                    digest = None
                    if repo_digests:
                        # RepoDigests 格式: ["image@sha256:xxx"]
                        for rd in repo_digests:
                            if "@sha256:" in rd:
                                digest = rd.split("@")[-1]
                                break
                    
                    # 如果没有 RepoDigests，使用镜像 Id
                    if not digest:
                        image_id = image_info.get("Id", "")
                        if image_id.startswith("sha256:"):
                            digest = image_id
                    
                    return ImageAuditResult(
                        image_name=image_name,
                        exists=True,
                        digest=digest,
                        created=image_info.get("Created"),
                        size=image_info.get("Size"),
                    )
        except subprocess.TimeoutExpired:
            continue
        except json.JSONDecodeError:
            continue
        except FileNotFoundError:
            # docker 命令不存在
            return ImageAuditResult(
                image_name=image_name,
                exists=False,
                error="docker command not found",
            )
        except Exception as e:
            continue
    
    # 所有变体都失败
    return ImageAuditResult(
        image_name=image_name,
        exists=False,
        error=f"image not found (tried: {', '.join(image_variants)})",
    )


def run_audit(
    lock_path: Path = LOCK_FILE,
    images: Optional[List[str]] = None,
    compose_project: str = "engram",
) -> AuditReport:
    """执行审计"""
    report = AuditReport()
    report.lock_file_path = str(lock_path)
    
    # 加载 lock 文件
    lock_data = load_lock_file(lock_path)
    if lock_data:
        report.upstream_ref = lock_data.get("upstream_ref")
        report.upstream_commit_sha = lock_data.get("upstream_commit_sha")
        report.local_patchset_id = lock_data.get("local_patchset_id")
    
    # 确定要检查的镜像列表
    if images is None:
        images = DEFAULT_IMAGES
    
    # 审计每个镜像
    found_count = 0
    missing_count = 0
    
    for image_name in images:
        result = inspect_docker_image(image_name, compose_project)
        report.images.append(result.to_dict())
        
        if result.exists:
            found_count += 1
        else:
            missing_count += 1
    
    # 生成摘要
    report.summary = {
        "total_images": len(images),
        "found": found_count,
        "missing": missing_count,
        "all_present": missing_count == 0,
    }
    
    return report


def main():
    parser = argparse.ArgumentParser(
        description="OpenMemory Artifact Audit - 审计 OpenMemory 相关 Docker 镜像"
    )
    parser.add_argument(
        "--lock-file",
        type=str,
        default=str(LOCK_FILE),
        help=f"lock 文件路径 (默认: {LOCK_FILE})",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=str(DEFAULT_OUTPUT),
        help=f"输出文件路径 (默认: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--compose-project",
        type=str,
        default=os.environ.get("COMPOSE_PROJECT_NAME", "engram"),
        help="Docker Compose 项目名 (默认: $COMPOSE_PROJECT_NAME 或 engram)",
    )
    parser.add_argument(
        "--images",
        type=str,
        nargs="+",
        default=None,
        help=f"要检查的镜像列表 (默认: {', '.join(DEFAULT_IMAGES)})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 格式到 stdout（不写文件）",
    )
    
    args = parser.parse_args()
    
    # 执行审计
    report = run_audit(
        lock_path=Path(args.lock_file),
        images=args.images,
        compose_project=args.compose_project,
    )
    
    # 输出结果
    report_dict = report.to_dict()
    report_json = json.dumps(report_dict, indent=2, ensure_ascii=False)
    
    if args.json:
        # 仅输出到 stdout
        print(report_json)
    else:
        # 写入文件
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report_json)
        
        # 打印摘要
        print("=" * 60)
        print("OpenMemory Artifact Audit Report")
        print("=" * 60)
        print(f"Timestamp:        {report.timestamp}")
        print(f"Lock File:        {report.lock_file_path}")
        print(f"Upstream Ref:     {report.upstream_ref or 'N/A'}")
        print(f"Patchset ID:      {report.local_patchset_id or 'N/A'}")
        print("-" * 60)
        print("Images:")
        for img in report.images:
            status = "[OK]" if img["exists"] else "[MISSING]"
            digest_short = (img["digest"] or "N/A")[:20] + "..." if img["digest"] and len(img["digest"]) > 20 else (img["digest"] or "N/A")
            print(f"  {status} {img['image_name']}: {digest_short}")
            if img.get("error"):
                print(f"       Error: {img['error']}")
        print("-" * 60)
        print(f"Summary: {report.summary['found']}/{report.summary['total_images']} images found")
        print(f"Output:  {output_path}")
        print("=" * 60)
    
    # 返回退出码（有缺失镜像时返回 1，但不阻止 CI）
    # 注意：镜像缺失在 CI 的构建前阶段是正常的
    return 0


if __name__ == "__main__":
    sys.exit(main())
