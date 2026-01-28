#!/usr/bin/env python3
"""
OpenMemory 上游版本漂移检查脚本

读取 OpenMemory.upstream.lock.json 中的 upstream_url/upstream_ref，
调用 GitHub API 获取最新 tags/releases，输出漂移报告。

输出文件: .artifacts/openmemory-upstream-drift.json
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def parse_github_url(url: str) -> tuple[str, str]:
    """
    解析 GitHub URL，提取 owner 和 repo。
    
    支持格式:
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    """
    pattern = r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(f"无法解析 GitHub URL: {url}")
    return match.group(1), match.group(2)


def github_api_get(endpoint: str, timeout: int = 30) -> Optional[dict]:
    """
    调用 GitHub API（无需认证的公共接口）。
    
    Args:
        endpoint: API 端点，如 /repos/owner/repo/tags
        timeout: 超时时间（秒）
    
    Returns:
        API 响应 JSON，失败时返回 None
    """
    url = f"https://api.github.com{endpoint}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "engram-upstream-drift-check/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    
    req = Request(url, headers=headers)
    
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        print(f"[WARN] GitHub API HTTP 错误: {e.code} {e.reason}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"[WARN] GitHub API 网络错误: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[WARN] GitHub API 未知错误: {e}", file=sys.stderr)
        return None


def parse_semver(version: str) -> tuple[int, int, int, str]:
    """
    解析语义化版本号，返回 (major, minor, patch, prerelease)。
    支持 v 前缀和预发布版本。
    """
    # 移除 v 前缀
    ver = version.lstrip("v")
    
    # 匹配 major.minor.patch[-prerelease]
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", ver)
    if match:
        return (
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            match.group(4) or "",
        )
    
    # 无法解析，返回低优先级
    return (0, 0, 0, version)


def compare_versions(current: str, latest: str) -> dict:
    """
    比较两个版本号，判断是否落后以及落后程度。
    
    Returns:
        {
            "is_behind": bool,
            "current_parsed": tuple,
            "latest_parsed": tuple,
            "drift_level": "major" | "minor" | "patch" | "none",
        }
    """
    cur = parse_semver(current)
    lat = parse_semver(latest)
    
    result = {
        "is_behind": False,
        "current_parsed": list(cur[:3]),
        "latest_parsed": list(lat[:3]),
        "drift_level": "none",
    }
    
    if lat > cur:
        result["is_behind"] = True
        if lat[0] > cur[0]:
            result["drift_level"] = "major"
        elif lat[1] > cur[1]:
            result["drift_level"] = "minor"
        elif lat[2] > cur[2]:
            result["drift_level"] = "patch"
    
    return result


def determine_priority(
    drift_level: str,
    is_security_release: bool,
    days_behind: Optional[int] = None,
) -> str:
    """
    根据漂移情况确定建议优先级。
    
    Returns:
        "security" | "high" | "normal" | "low" | "none"
    """
    if is_security_release:
        return "security"
    
    if drift_level == "major":
        return "high"
    elif drift_level == "minor":
        return "normal"
    elif drift_level == "patch":
        return "low"
    
    return "none"


def check_security_keywords(release_body: str) -> bool:
    """检查 release notes 是否包含安全相关关键词。"""
    if not release_body:
        return False
    
    keywords = [
        "security",
        "vulnerability",
        "CVE-",
        "exploit",
        "critical fix",
        "安全",
        "漏洞",
    ]
    body_lower = release_body.lower()
    return any(kw.lower() in body_lower for kw in keywords)


def main():
    """主函数"""
    # 确定项目根目录
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    
    lock_file = project_root / "OpenMemory.upstream.lock.json"
    output_dir = project_root / ".artifacts"
    output_file = output_dir / "openmemory-upstream-drift.json"
    
    # 读取 lock 文件
    if not lock_file.exists():
        print(f"[ERROR] Lock 文件不存在: {lock_file}", file=sys.stderr)
        sys.exit(1)
    
    with open(lock_file, "r", encoding="utf-8") as f:
        lock_data = json.load(f)
    
    upstream_url = lock_data.get("upstream_url")
    upstream_ref = lock_data.get("upstream_ref")
    upstream_ref_type = lock_data.get("upstream_ref_type", "tag")
    
    if not upstream_url or not upstream_ref:
        print("[ERROR] Lock 文件缺少 upstream_url 或 upstream_ref", file=sys.stderr)
        sys.exit(1)
    
    print(f"当前上游: {upstream_url}")
    print(f"当前版本: {upstream_ref} (type: {upstream_ref_type})")
    
    # 解析 GitHub URL
    try:
        owner, repo = parse_github_url(upstream_url)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    
    print(f"GitHub 仓库: {owner}/{repo}")
    
    # 准备输出结构
    report = {
        "check_timestamp": datetime.now(timezone.utc).isoformat(),
        "upstream_url": upstream_url,
        "upstream_owner": owner,
        "upstream_repo": repo,
        "current_ref": upstream_ref,
        "current_ref_type": upstream_ref_type,
        "latest_ref": None,
        "latest_ref_type": None,
        "is_behind": False,
        "drift_level": "none",
        "priority": "none",
        "api_success": False,
        "api_error": None,
        "releases": [],
        "tags": [],
    }
    
    # 获取最新 releases
    print("\n获取最新 releases...")
    releases_data = github_api_get(f"/repos/{owner}/{repo}/releases?per_page=10")
    
    latest_release = None
    is_security_release = False
    
    if releases_data:
        report["api_success"] = True
        report["releases"] = [
            {
                "tag_name": r.get("tag_name"),
                "name": r.get("name"),
                "prerelease": r.get("prerelease", False),
                "draft": r.get("draft", False),
                "published_at": r.get("published_at"),
            }
            for r in releases_data[:5]
        ]
        
        # 找到最新的非预发布、非草稿 release
        for rel in releases_data:
            if not rel.get("prerelease") and not rel.get("draft"):
                latest_release = rel
                # 检查是否是安全更新
                body = rel.get("body", "") or ""
                is_security_release = check_security_keywords(body)
                break
        
        if latest_release:
            report["latest_ref"] = latest_release["tag_name"]
            report["latest_ref_type"] = "release"
            print(f"最新 release: {latest_release['tag_name']}")
    else:
        print("[WARN] 无法获取 releases，尝试获取 tags...")
    
    # 如果没有 release，降级到 tags
    if not latest_release:
        print("获取最新 tags...")
        tags_data = github_api_get(f"/repos/{owner}/{repo}/tags?per_page=10")
        
        if tags_data:
            report["api_success"] = True
            report["tags"] = [{"name": t.get("name")} for t in tags_data[:5]]
            
            # 找到最新的语义化版本 tag
            for tag in tags_data:
                tag_name = tag.get("name", "")
                # 优先选择 v 开头的语义化版本
                if re.match(r"^v?\d+\.\d+\.\d+", tag_name):
                    report["latest_ref"] = tag_name
                    report["latest_ref_type"] = "tag"
                    print(f"最新 tag: {tag_name}")
                    break
        else:
            report["api_error"] = "无法获取 releases 和 tags"
            print("[WARN] 无法获取 tags")
    
    # 比较版本
    if report["latest_ref"]:
        comparison = compare_versions(upstream_ref, report["latest_ref"])
        report["is_behind"] = comparison["is_behind"]
        report["drift_level"] = comparison["drift_level"]
        report["priority"] = determine_priority(
            comparison["drift_level"],
            is_security_release,
        )
        
        print(f"\n版本比较:")
        print(f"  当前: {upstream_ref}")
        print(f"  最新: {report['latest_ref']}")
        print(f"  是否落后: {report['is_behind']}")
        print(f"  漂移级别: {report['drift_level']}")
        print(f"  建议优先级: {report['priority']}")
        if is_security_release:
            print("  ⚠️  最新版本包含安全修复!")
    else:
        print("\n[WARN] 无法确定最新版本，跳过版本比较")
    
    # 确保输出目录存在
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 写入报告
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n报告已写入: {output_file}")
    
    # 根据优先级返回不同退出码（便于 CI 判断）
    # 0: 无漂移或低优先级
    # 1: 安全更新（需要紧急关注）
    # 2: 高优先级（major 版本落后）
    if report["priority"] == "security":
        print("\n[ALERT] 检测到安全更新，建议尽快评估升级!")
        sys.exit(1)
    elif report["priority"] == "high":
        print("\n[WARN] 检测到 major 版本落后，建议评估升级计划")
        sys.exit(0)  # 不阻塞 CI
    
    sys.exit(0)


if __name__ == "__main__":
    main()
