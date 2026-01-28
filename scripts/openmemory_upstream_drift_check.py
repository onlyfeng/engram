#!/usr/bin/env python3
"""
OpenMemory 上游版本漂移检查脚本

读取 OpenMemory.upstream.lock.json 中的 upstream_url/upstream_ref，
调用 GitHub API 获取最新 tags/releases，输出漂移报告。

功能增强 (2026-01):
- 冻结条件检测：读取 freeze_status 和 freeze_rules
- Security 优先级：输出详细 summary + artifact
- CI 集成：支持 OPENMEMORY_FREEZE_OVERRIDE 环境变量进行人工 override

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


# 环境变量：人工 override 冻结
FREEZE_OVERRIDE_ENV = "OPENMEMORY_FREEZE_OVERRIDE"
FREEZE_OVERRIDE_REASON_ENV = "OPENMEMORY_FREEZE_OVERRIDE_REASON"


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


def check_freeze_status(lock_data: dict) -> dict:
    """
    检查冻结状态。
    
    Returns:
        {
            "is_frozen": bool,
            "freeze_reason": str | None,
            "freeze_expires_at": str | None,
            "override_requested": bool,
            "override_reason": str | None,
            "override_valid": bool,
            "freeze_message": str,
        }
    """
    result = {
        "is_frozen": False,
        "freeze_reason": None,
        "freeze_expires_at": None,
        "override_requested": False,
        "override_reason": None,
        "override_valid": False,
        "freeze_message": "",
    }
    
    # 检查 freeze_status
    freeze_status = lock_data.get("freeze_status", {})
    if freeze_status.get("is_frozen"):
        result["is_frozen"] = True
        result["freeze_reason"] = freeze_status.get("freeze_reason")
        result["freeze_expires_at"] = freeze_status.get("freeze_expires_at")
        
        # 检查是否过期
        expires_at = freeze_status.get("freeze_expires_at")
        if expires_at:
            try:
                expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) > expires_dt:
                    result["is_frozen"] = False
                    result["freeze_message"] = f"冻结已过期（{expires_at}）"
                else:
                    result["freeze_message"] = f"处于冻结状态：{result['freeze_reason']}（到期：{expires_at}）"
            except ValueError:
                result["freeze_message"] = f"处于冻结状态：{result['freeze_reason']}"
        else:
            result["freeze_message"] = f"处于冻结状态：{result['freeze_reason']}（需人工解除）"
    
    # 检查环境变量 override
    override_value = os.environ.get(FREEZE_OVERRIDE_ENV, "").lower()
    if override_value in ("true", "1", "yes"):
        result["override_requested"] = True
        result["override_reason"] = os.environ.get(FREEZE_OVERRIDE_REASON_ENV, "CI manual override")
        
        # override 有效条件：必须提供原因
        if result["override_reason"] and len(result["override_reason"]) > 5:
            result["override_valid"] = True
            if result["is_frozen"]:
                result["freeze_message"] = f"冻结已被 override：{result['override_reason']}"
        else:
            result["freeze_message"] = "Override 请求无效：必须通过 OPENMEMORY_FREEZE_OVERRIDE_REASON 提供详细原因"
    
    return result


def generate_security_summary(report: dict) -> str:
    """
    生成安全更新的详细摘要（用于 GitHub Summary/Issue）。
    """
    lines = [
        "## 🚨 OpenMemory 上游安全更新检测",
        "",
        "### 版本信息",
        f"- **当前版本**: `{report['current_ref']}`",
        f"- **最新版本**: `{report.get('latest_ref', 'N/A')}`",
        f"- **漂移级别**: `{report['drift_level']}`",
        "",
        "### 检测详情",
        f"- **检测时间**: {report['check_timestamp']}",
        f"- **上游仓库**: {report['upstream_url']}",
        "",
        "### 建议操作",
        "1. 查看 `.artifacts/openmemory-upstream-drift.json` 获取完整报告",
        "2. 评估安全更新内容和影响",
        "3. 执行 `make openmemory-upgrade-check` 进行升级验证",
        "4. 如需升级: `make openmemory-upgrade-prod`",
        "",
        "### 自动化处理",
        "- 此检测由 Nightly CI 自动执行",
        "- 安全更新将自动创建高优先级 Issue",
        "- 详情请查看 workflow run artifacts",
    ]
    
    # 如果有 releases 信息
    if report.get("releases"):
        lines.extend([
            "",
            "### 最近 Releases",
        ])
        for rel in report["releases"][:3]:
            tag = rel.get("tag_name", "N/A")
            name = rel.get("name", "")
            published = rel.get("published_at", "")[:10] if rel.get("published_at") else ""
            lines.append(f"- `{tag}` - {name} ({published})")
    
    return "\n".join(lines)


def main():
    """主函数"""
    # 确定项目根目录
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    
    lock_file = project_root / "OpenMemory.upstream.lock.json"
    output_dir = project_root / ".artifacts"
    output_file = output_dir / "openmemory-upstream-drift.json"
    security_summary_file = output_dir / "openmemory-security-summary.md"
    
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
    
    # 检查冻结状态
    freeze_check = check_freeze_status(lock_data)
    if freeze_check["is_frozen"]:
        print(f"\n⚠️  {freeze_check['freeze_message']}")
        if freeze_check["override_requested"]:
            if freeze_check["override_valid"]:
                print(f"✅ Override 有效: {freeze_check['override_reason']}")
            else:
                print(f"❌ {freeze_check['freeze_message']}")
                print(f"\n要进行 override，请设置环境变量:")
                print(f"  export {FREEZE_OVERRIDE_ENV}=true")
                print(f"  export {FREEZE_OVERRIDE_REASON_ENV}='详细原因'")
                sys.exit(3)  # 冻结且无有效 override
    
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
        # 冻结状态
        "freeze_status": {
            "is_frozen": freeze_check["is_frozen"],
            "freeze_reason": freeze_check["freeze_reason"],
            "freeze_expires_at": freeze_check["freeze_expires_at"],
            "override_requested": freeze_check["override_requested"],
            "override_valid": freeze_check["override_valid"],
            "override_reason": freeze_check["override_reason"],
        },
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
    
    # ========================================================================
    # Exit Code 语义 (2026-01 更新)
    # ========================================================================
    # 0: 无漂移、低/正常/高优先级（不阻断 CI）
    # 1: 安全更新检测到（非阻断，但触发通知/Issue 创建）
    # 3: 冻结状态且无有效 override（可选阻断后续升级步骤）
    #
    # 注意: exit=2 未使用（保留以备将来扩展）
    #
    # ========================================================================
    # 冻结状态与 Security 的交互逻辑
    # ========================================================================
    # - freeze + 非 security: exit=3，可阻断后续升级步骤
    # - freeze + security: exit=1，不阻断但在 Summary 中警告需要 override
    # - security 检测后会输出 .artifacts/openmemory-security-summary.md
    #
    # ========================================================================
    # CI 集成说明
    # ========================================================================
    # - exit=1 时 CI 会在 Summary 中突出显示 security alert，并创建 Issue
    # - exit=3 时 CI 提示需要人工 override，可通过 needs_override 输出阻断后续步骤
    # - 配合 continue-on-error: true 保持非阻塞
    # - 通过 GITHUB_OUTPUT 传递状态供后续 steps 使用
    # - 使用 scripts/openmemory_drift_parse.py 统一解析 drift report JSON
    # ========================================================================
    
    # 输出 CI 友好的状态信息
    print("\n" + "=" * 50)
    print("CI Integration Summary")
    print("=" * 50)
    print(f"  priority: {report['priority']}")
    print(f"  is_behind: {report['is_behind']}")
    print(f"  drift_level: {report['drift_level']}")
    print(f"  latest_ref: {report.get('latest_ref', 'N/A')}")
    print(f"  is_frozen: {freeze_check['is_frozen']}")
    if freeze_check["override_requested"]:
        print(f"  override_valid: {freeze_check['override_valid']}")
    
    if report["priority"] == "security":
        print("\n" + "!" * 50)
        print("!!! SECURITY UPDATE DETECTED !!!")
        print("!" * 50)
        print("\n[ALERT] 检测到安全更新，建议尽快评估升级!")
        
        # 生成并保存安全摘要（用于 GitHub Summary/Issue）
        security_summary = generate_security_summary(report)
        report["security_summary"] = security_summary
        
        # 更新报告文件（包含 security_summary）
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        # 写入单独的 markdown 摘要文件（便于 CI 上传为 artifact）
        with open(security_summary_file, "w", encoding="utf-8") as f:
            f.write(security_summary)
        print(f"\n安全摘要已写入: {security_summary_file}")
        
        print("\n建议操作:")
        print("  1. 查看 .artifacts/openmemory-upstream-drift.json")
        print("  2. 查看 .artifacts/openmemory-security-summary.md")
        print("  3. 评估安全更新内容和影响")
        print("  4. 执行 make openmemory-upgrade-check 进行升级验证")
        print("  5. 如需升级: make openmemory-upgrade-prod")
        
        # 检查冻结状态（security 优先级时仍需检查）
        if freeze_check["is_frozen"] and not freeze_check["override_valid"]:
            print("\n" + "⚠" * 25)
            print("警告：当前处于冻结状态，安全升级需要人工 override")
            print("⚠" * 25)
            print(f"\n冻结原因: {freeze_check['freeze_reason']}")
            print(f"\n要进行 security override，请设置环境变量:")
            print(f"  export {FREEZE_OVERRIDE_ENV}=true")
            print(f"  export {FREEZE_OVERRIDE_REASON_ENV}='Security update for [CVE-XXXX]'")
            # 安全更新 + 冻结：exit=1 触发通知，但不阻塞
        
        sys.exit(1)
    elif report["priority"] == "high":
        print("\n[WARN] 检测到 major 版本落后，建议评估升级计划")
        sys.exit(0)  # 不阻塞 CI
    elif report["priority"] == "normal":
        print("\n[INFO] 检测到 minor 版本落后，可计划升级")
        sys.exit(0)
    elif report["priority"] == "low":
        print("\n[INFO] 检测到 patch 版本落后，低优先级")
        sys.exit(0)
    else:
        print("\n[OK] 版本为最新或无法确定")
        sys.exit(0)


if __name__ == "__main__":
    main()
