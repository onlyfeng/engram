#!/usr/bin/env python3
"""
OpenMemory Sync Report 解析辅助脚本

统一处理 openmemory_sync.py 的 check/apply/verify 报告 JSON 解析，
替代 workflow 中分散的 grep/inline Python 解析逻辑。

用法:
    # 模式 1: 从单一 JSON 读取（支持 stdin）
    python scripts/openmemory_sync.py check --json | python scripts/openmemory_sync_parse.py -
    python scripts/openmemory_sync_parse.py report.json --github-output FILE
    
    # 模式 2: 从多个文件读取（CI 场景）
    python scripts/openmemory_sync_parse.py --check check.json --apply apply.json --verify verify.json

输出 (JSON 到 stdout，key=value 到 GITHUB_OUTPUT):
    overall_status              整体状态 (ok|warn|error)
    check_overall_status        检查阶段整体状态 (ok|warn|error|N/A)
    apply_final_status          应用阶段最终状态 (ok|warn|error|N/A)
    verify_final_status         验证阶段最终状态 (ok|warn|error|N/A)
    category_mismatch_A         Category A 不匹配数量
    category_mismatch_B         Category B 不匹配数量
    category_mismatch_C         Category C 不匹配数量
    conflict_files_count        冲突文件数量
    lock_update_blocked         lock 更新是否被阻止 (true|false)
    strict_patch_files          是否启用严格 patch 文件模式 (true|false)
    missing_patch_files_count   缺失的 patch 文件数量
    bundle_sha256_status        bundle SHA256 状态 (verified|mismatch|null|no_files|unknown)
    should_fail                 是否应该失败 (true|false)
    should_warn                 是否应该警告 (true|false)
    summary                     摘要文本

Fail/Warn 策略:
    - verify_final_status==error 或 category_mismatch_A>0 => fail
    - category_mismatch_B>0 => warn (不 fail)
    - category_mismatch_C>0 => 仅记录 (不 warn 不 fail)
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def read_json_source(source: str) -> dict:
    """
    从文件或 stdin 读取 JSON。
    
    Args:
        source: 文件路径或 "-" 表示 stdin
    
    Returns:
        解析后的 JSON 数据
    
    Raises:
        Exception: 读取或解析失败
    """
    if source == "-":
        content = sys.stdin.read()
    else:
        with open(source, "r", encoding="utf-8") as f:
            content = f.read()
    return json.loads(content)


def parse_unified_report(data: dict) -> dict:
    """
    解析统一的报告 JSON（openmemory_sync.py --json 输出），
    提取所有需要的字段。
    
    Returns:
        包含所有解析字段的字典
    """
    result = {
        # 基础状态
        "overall_status": data.get("overall_status", "unknown"),
        "check_overall_status": "N/A",
        "apply_final_status": "N/A",
        "verify_final_status": "N/A",
        
        # Category 不匹配计数（verify）
        "category_mismatch_A": 0,
        "category_mismatch_B": 0,
        "category_mismatch_C": 0,
        
        # 冲突和阻断
        "conflict_files_count": 0,
        "conflict_artifacts_dir": data.get("conflict_artifacts_dir", ""),
        "lock_update_blocked": False,
        
        # Patch 文件相关
        "strict_patch_files": False,
        "missing_patch_files_count": 0,
        "bundle_sha256_status": "unknown",
        
        # 解析元数据
        "parse_error": None,
    }
    
    # 从 checks 推断 check_overall_status
    checks = data.get("checks", [])
    if checks:
        check_statuses = [c.get("status", "ok") for c in checks]
        if "error" in check_statuses:
            result["check_overall_status"] = "error"
        elif "warn" in check_statuses:
            result["check_overall_status"] = "warn"
        else:
            result["check_overall_status"] = "ok"
    
    # 提取 patches_status
    patches_status = data.get("patches_status", {})
    
    # 提取 patch_files_check
    patch_files_check = patches_status.get("patch_files_check", {})
    if patch_files_check:
        result["missing_patch_files_count"] = patch_files_check.get("missing", 0)
        result["strict_patch_files"] = patch_files_check.get("strict_mode", False)
        bundle_sha256 = patch_files_check.get("bundle_sha256", {})
        result["bundle_sha256_status"] = bundle_sha256.get("status", "unknown")
    
    # 提取 apply_result
    apply_result = patches_status.get("apply_result", {})
    if apply_result:
        result["apply_final_status"] = apply_result.get("final_status", "N/A")
        result["lock_update_blocked"] = apply_result.get("lock_update_blocked", False)
        
        # 从 apply_result 获取冲突数
        apply_summary = apply_result.get("summary", {})
        result["conflict_files_count"] = apply_summary.get("conflict_files_count", 0)
        if result["conflict_files_count"] == 0 and "conflict_files" in apply_result:
            result["conflict_files_count"] = len(apply_result.get("conflict_files", []))
    
    # 提取 verify_result
    verify_result = patches_status.get("verify_result", {})
    if verify_result:
        result["verify_final_status"] = verify_result.get("final_status", "N/A")
        
        category_mismatch = verify_result.get("category_mismatch", {})
        result["category_mismatch_A"] = category_mismatch.get("A", 0)
        result["category_mismatch_B"] = category_mismatch.get("B", 0)
        result["category_mismatch_C"] = category_mismatch.get("C", 0)
        
        # 如果 apply_result 没有冲突信息，从 verify_result 获取
        if result["conflict_files_count"] == 0:
            verify_summary = verify_result.get("summary", {})
            result["conflict_files_count"] = verify_summary.get("conflict_files_count", 0)
    
    # 从报告的顶层冲突字段补充
    conflict_files = data.get("conflict_files", [])
    if conflict_files and result["conflict_files_count"] == 0:
        result["conflict_files_count"] = len(conflict_files)
    
    # 环境变量覆盖 strict_patch_files
    strict_env = os.environ.get("OPENMEMORY_PATCH_FILES_REQUIRED", "")
    if strict_env.lower() in ("1", "true", "yes"):
        result["strict_patch_files"] = True
    
    return result


def parse_verify_report(report_path: str) -> dict:
    """
    解析 verify report JSON 文件，返回统一结构。
    
    Returns:
        {
            "verify_final_status": str,
            "category_mismatch_A": int,
            "category_mismatch_B": int,
            "category_mismatch_C": int,
            "missing_count": int,
            "conflict_files_count": int,
            "conflict_files": list,
            "conflict_artifacts_dir": str,
            "strict_patch_files": bool,
            "missing_patch_files_count": int,
            "bundle_sha256_status": str,
            "parse_error": str | None,
        }
    """
    result = {
        "verify_final_status": "unknown",
        "category_mismatch_A": 0,
        "category_mismatch_B": 0,
        "category_mismatch_C": 0,
        "missing_count": 0,
        "conflict_files_count": 0,
        "conflict_files": [],
        "conflict_artifacts_dir": "",
        "strict_patch_files": False,
        "missing_patch_files_count": 0,
        "bundle_sha256_status": "unknown",
        "parse_error": None,
    }
    
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        result["parse_error"] = f"Report file not found: {report_path}"
        return result
    except json.JSONDecodeError as e:
        result["parse_error"] = f"Invalid JSON: {e}"
        return result
    except Exception as e:
        result["parse_error"] = f"Unexpected error: {e}"
        return result
    
    # 提取 patches_status
    patches_status = data.get("patches_status", {})
    
    # 提取 patch_files_check (新增)
    patch_files_check = patches_status.get("patch_files_check", {})
    if patch_files_check:
        result["missing_patch_files_count"] = patch_files_check.get("missing", 0)
        result["strict_patch_files"] = patch_files_check.get("strict_mode", False)
        bundle_sha256 = patch_files_check.get("bundle_sha256", {})
        result["bundle_sha256_status"] = bundle_sha256.get("status", "unknown")
    
    # 提取 verify_result
    verify_result = patches_status.get("verify_result", {})
    
    # final_status
    result["verify_final_status"] = verify_result.get("final_status", "unknown")
    
    # category_mismatch
    category_mismatch = verify_result.get("category_mismatch", {})
    result["category_mismatch_A"] = category_mismatch.get("A", 0)
    result["category_mismatch_B"] = category_mismatch.get("B", 0)
    result["category_mismatch_C"] = category_mismatch.get("C", 0)
    
    # summary
    summary = verify_result.get("summary", {})
    result["missing_count"] = summary.get("missing", 0)
    result["conflict_files_count"] = summary.get("conflict_files_count", 0)
    
    # conflict files
    result["conflict_files"] = verify_result.get("conflict_files", [])
    
    # conflict_artifacts_dir (顶层字段)
    result["conflict_artifacts_dir"] = data.get("conflict_artifacts_dir", "")
    
    # strict_patch_files 环境变量（从环境读取，非 JSON）
    strict_env = os.environ.get("OPENMEMORY_PATCH_FILES_REQUIRED", "")
    if strict_env.lower() in ("1", "true", "yes"):
        result["strict_patch_files"] = True
    
    return result


def parse_apply_report(report_path: str) -> dict:
    """
    解析 apply report JSON 文件。
    """
    result = {
        "apply_final_status": "unknown",
        "apply_conflicts_A": 0,
        "apply_conflicts_B": 0,
        "apply_conflicts_C": 0,
        "lock_update_blocked": False,
        "force_update_lock": False,
        "conflict_files_count": 0,
        "parse_error": None,
    }
    
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        result["parse_error"] = f"Report file not found: {report_path}"
        return result
    except json.JSONDecodeError as e:
        result["parse_error"] = f"Invalid JSON: {e}"
        return result
    except Exception as e:
        result["parse_error"] = f"Unexpected error: {e}"
        return result
    
    patches_status = data.get("patches_status", {})
    apply_result = patches_status.get("apply_result", {})
    
    result["apply_final_status"] = apply_result.get("final_status", "unknown")
    
    conflicts_by_cat = apply_result.get("conflicts_by_category", {})
    result["apply_conflicts_A"] = len(conflicts_by_cat.get("A", []))
    result["apply_conflicts_B"] = len(conflicts_by_cat.get("B", []))
    result["apply_conflicts_C"] = len(conflicts_by_cat.get("C", []))
    
    result["lock_update_blocked"] = apply_result.get("lock_update_blocked", False)
    result["force_update_lock"] = apply_result.get("force_update_lock", False)
    
    # 冲突文件数量
    summary = apply_result.get("summary", {})
    result["conflict_files_count"] = summary.get("conflict_files_count", 0)
    
    return result


def parse_check_report(report_path: str) -> dict:
    """
    解析 check report JSON 文件。
    """
    result = {
        "check_overall_status": "unknown",
        "missing_patch_files_count": 0,
        "bundle_sha256_status": "unknown",
        "strict_patch_files": False,
        "parse_error": None,
    }
    
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        result["parse_error"] = f"Report file not found: {report_path}"
        return result
    except json.JSONDecodeError as e:
        result["parse_error"] = f"Invalid JSON: {e}"
        return result
    except Exception as e:
        result["parse_error"] = f"Unexpected error: {e}"
        return result
    
    result["check_overall_status"] = data.get("overall_status", "unknown")
    
    # 提取 patch_files_check
    patches_status = data.get("patches_status", {})
    patch_files_check = patches_status.get("patch_files_check", {})
    if patch_files_check:
        result["missing_patch_files_count"] = patch_files_check.get("missing", 0)
        result["strict_patch_files"] = patch_files_check.get("strict_mode", False)
        bundle_sha256 = patch_files_check.get("bundle_sha256", {})
        result["bundle_sha256_status"] = bundle_sha256.get("status", "unknown")
    
    # 环境变量覆盖 strict_patch_files
    strict_env = os.environ.get("OPENMEMORY_PATCH_FILES_REQUIRED", "")
    if strict_env.lower() in ("1", "true", "yes"):
        result["strict_patch_files"] = True
    
    return result


def determine_actions(verify_result: dict, apply_result: dict, check_result: dict) -> dict:
    """
    根据解析结果确定 fail/warn 动作。
    
    策略:
        - verify_final_status==error 或 category_mismatch_A>0 => fail
        - apply_final_status==error 或 apply_conflicts_A>0 => fail
        - check_overall_status==error => fail
        - force_update_lock==true (CI 中不应使用) => fail
        - category_mismatch_B>0 => warn (不 fail)
        - apply_conflicts_B>0 => warn (不 fail)
    """
    should_fail = False
    should_warn = False
    fail_reasons = []
    warn_reasons = []
    
    # Check 阶段
    if check_result.get("check_overall_status") == "error":
        should_fail = True
        fail_reasons.append("check overall_status=error")
    
    # Apply 阶段
    if apply_result.get("apply_final_status") == "error":
        should_fail = True
        fail_reasons.append("apply final_status=error")
    
    if apply_result.get("apply_conflicts_A", 0) > 0:
        should_fail = True
        fail_reasons.append(f"apply Category A conflicts: {apply_result['apply_conflicts_A']}")
    
    if apply_result.get("apply_conflicts_B", 0) > 0:
        should_warn = True
        warn_reasons.append(f"apply Category B conflicts: {apply_result['apply_conflicts_B']}")
    
    if apply_result.get("force_update_lock"):
        should_fail = True
        fail_reasons.append("force_update_lock=true (不应在 CI 中使用)")
    
    # Verify 阶段
    if verify_result.get("verify_final_status") == "error":
        should_fail = True
        fail_reasons.append("verify final_status=error")
    
    if verify_result.get("category_mismatch_A", 0) > 0:
        should_fail = True
        fail_reasons.append(f"verify Category A mismatch: {verify_result['category_mismatch_A']}")
    
    if verify_result.get("missing_count", 0) > 0:
        should_fail = True
        fail_reasons.append(f"verify missing files: {verify_result['missing_count']}")
    
    if verify_result.get("category_mismatch_B", 0) > 0:
        should_warn = True
        warn_reasons.append(f"verify Category B mismatch: {verify_result['category_mismatch_B']}")
    
    # 生成摘要
    summary_parts = []
    if should_fail:
        summary_parts.append(f"FAIL: {'; '.join(fail_reasons)}")
    if should_warn:
        summary_parts.append(f"WARN: {'; '.join(warn_reasons)}")
    if not should_fail and not should_warn:
        summary_parts.append("OK: All checks passed")
    
    # 计算 overall_status
    if should_fail:
        overall_status = "error"
    elif should_warn:
        overall_status = "warn"
    else:
        overall_status = "ok"
    
    return {
        "should_fail": should_fail,
        "should_warn": should_warn,
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "overall_status": overall_status,
        "summary": " | ".join(summary_parts),
    }


def format_github_output(
    verify_result: dict,
    apply_result: dict,
    check_result: dict,
    actions: dict
) -> str:
    """格式化为 GITHUB_OUTPUT 格式。"""
    lines = []
    
    # Verify 字段
    lines.append(f"verify_final_status={verify_result.get('verify_final_status', 'unknown')}")
    lines.append(f"category_mismatch_A={verify_result.get('category_mismatch_A', 0)}")
    lines.append(f"category_mismatch_B={verify_result.get('category_mismatch_B', 0)}")
    lines.append(f"category_mismatch_C={verify_result.get('category_mismatch_C', 0)}")
    lines.append(f"missing_count={verify_result.get('missing_count', 0)}")
    lines.append(f"conflict_files_count={verify_result.get('conflict_files_count', apply_result.get('conflict_files_count', 0))}")
    lines.append(f"conflict_artifacts_dir={verify_result.get('conflict_artifacts_dir', '')}")
    lines.append(f"strict_patch_files={'true' if check_result.get('strict_patch_files') or verify_result.get('strict_patch_files') else 'false'}")
    
    # Patch 文件相关（新增）
    lines.append(f"missing_patch_files_count={check_result.get('missing_patch_files_count', verify_result.get('missing_patch_files_count', 0))}")
    lines.append(f"bundle_sha256_status={check_result.get('bundle_sha256_status', verify_result.get('bundle_sha256_status', 'unknown'))}")
    
    # Apply 字段
    lines.append(f"apply_final_status={apply_result.get('apply_final_status', 'unknown')}")
    lines.append(f"apply_conflicts_A={apply_result.get('apply_conflicts_A', 0)}")
    lines.append(f"apply_conflicts_B={apply_result.get('apply_conflicts_B', 0)}")
    lines.append(f"lock_update_blocked={'true' if apply_result.get('lock_update_blocked') else 'false'}")
    lines.append(f"force_update_lock={'true' if apply_result.get('force_update_lock') else 'false'}")
    
    # Check 字段
    lines.append(f"check_overall_status={check_result.get('check_overall_status', 'unknown')}")
    
    # Action 字段
    lines.append(f"overall_status={actions.get('overall_status', 'unknown')}")
    lines.append(f"should_fail={'true' if actions.get('should_fail') else 'false'}")
    lines.append(f"should_warn={'true' if actions.get('should_warn') else 'false'}")
    lines.append(f"summary={actions.get('summary', '')}")
    
    return "\n".join(lines)


def format_github_output_unified(result: dict, actions: dict) -> str:
    """格式化统一报告解析结果为 GITHUB_OUTPUT 格式。"""
    lines = []
    
    # 基础状态
    lines.append(f"overall_status={result.get('overall_status', 'unknown')}")
    lines.append(f"check_overall_status={result.get('check_overall_status', 'N/A')}")
    lines.append(f"apply_final_status={result.get('apply_final_status', 'N/A')}")
    lines.append(f"verify_final_status={result.get('verify_final_status', 'N/A')}")
    
    # Category 不匹配
    lines.append(f"category_mismatch_A={result.get('category_mismatch_A', 0)}")
    lines.append(f"category_mismatch_B={result.get('category_mismatch_B', 0)}")
    lines.append(f"category_mismatch_C={result.get('category_mismatch_C', 0)}")
    
    # 冲突和阻断
    lines.append(f"conflict_files_count={result.get('conflict_files_count', 0)}")
    lines.append(f"lock_update_blocked={'true' if result.get('lock_update_blocked') else 'false'}")
    
    # Patch 文件相关
    lines.append(f"strict_patch_files={'true' if result.get('strict_patch_files') else 'false'}")
    lines.append(f"missing_patch_files_count={result.get('missing_patch_files_count', 0)}")
    lines.append(f"bundle_sha256_status={result.get('bundle_sha256_status', 'unknown')}")
    
    # Action 字段
    lines.append(f"should_fail={'true' if actions.get('should_fail') else 'false'}")
    lines.append(f"should_warn={'true' if actions.get('should_warn') else 'false'}")
    lines.append(f"summary={actions.get('summary', '')}")
    
    return "\n".join(lines)


def generate_summary_markdown(
    verify_result: dict,
    apply_result: dict,
    check_result: dict,
    actions: dict
) -> str:
    """生成 GitHub Step Summary markdown。"""
    lines = []
    
    lines.append("### OpenMemory Sync 状态检查")
    lines.append("")
    
    # 状态概览
    overall = actions.get("overall_status", "unknown")
    if overall == "error":
        lines.append("> [!CAUTION]")
        lines.append("> **状态: ERROR** - 存在必须修复的问题")
    elif overall == "warn":
        lines.append("> [!WARNING]")
        lines.append("> **状态: WARN** - 存在需要关注的问题")
    else:
        lines.append("> [!NOTE]")
        lines.append("> **状态: OK** - 所有检查通过")
    
    lines.append("")
    lines.append("| 检查项 | 状态 | 详情 |")
    lines.append("|--------|------|------|")
    
    # Check 阶段
    check_status = check_result.get("check_overall_status", "unknown")
    check_icon = "✅" if check_status == "ok" else ("⚠️" if check_status == "warn" else "❌")
    lines.append(f"| Sync Check | {check_icon} {check_status} | - |")
    
    # Apply 阶段
    apply_status = apply_result.get("apply_final_status", "unknown")
    apply_icon = "✅" if apply_status == "ok" else ("⚠️" if apply_status == "warn" else "❌")
    apply_detail = f"A:{apply_result.get('apply_conflicts_A', 0)}, B:{apply_result.get('apply_conflicts_B', 0)}, C:{apply_result.get('apply_conflicts_C', 0)}"
    lines.append(f"| Sync Apply | {apply_icon} {apply_status} | Conflicts: {apply_detail} |")
    
    # Verify 阶段
    verify_status = verify_result.get("verify_final_status", "unknown")
    verify_icon = "✅" if verify_status == "ok" else ("⚠️" if verify_status == "warn" else "❌")
    verify_detail = f"A:{verify_result.get('category_mismatch_A', 0)}, B:{verify_result.get('category_mismatch_B', 0)}, C:{verify_result.get('category_mismatch_C', 0)}"
    lines.append(f"| Sync Verify | {verify_icon} {verify_status} | Mismatch: {verify_detail} |")
    
    # 冲突文件
    conflict_count = verify_result.get("conflict_files_count", 0)
    conflict_dir = verify_result.get("conflict_artifacts_dir", "")
    if conflict_count > 0 or conflict_dir:
        lines.append("")
        lines.append("**冲突详情:**")
        lines.append(f"- 冲突文件数: {conflict_count}")
        if conflict_dir:
            lines.append(f"- 冲突产物目录: `{conflict_dir}`")
    
    # strict_patch_files 状态
    strict = verify_result.get("strict_patch_files", False)
    lines.append("")
    lines.append(f"**strict_patch_files**: `{strict}` (OPENMEMORY_PATCH_FILES_REQUIRED)")
    
    # Fail/Warn 原因
    if actions.get("fail_reasons"):
        lines.append("")
        lines.append("**Fail 原因:**")
        for reason in actions["fail_reasons"]:
            lines.append(f"- {reason}")
    
    if actions.get("warn_reasons"):
        lines.append("")
        lines.append("**Warn 原因:**")
        for reason in actions["warn_reasons"]:
            lines.append(f"- {reason}")
    
    return "\n".join(lines)


def determine_actions_unified(result: dict) -> dict:
    """
    根据统一报告解析结果确定 fail/warn 动作。
    """
    should_fail = False
    should_warn = False
    fail_reasons = []
    warn_reasons = []
    
    # Check 阶段
    if result.get("check_overall_status") == "error":
        should_fail = True
        fail_reasons.append("check overall_status=error")
    
    # Apply 阶段
    if result.get("apply_final_status") == "error":
        should_fail = True
        fail_reasons.append("apply final_status=error")
    
    if result.get("lock_update_blocked"):
        should_warn = True
        warn_reasons.append("lock_update_blocked=true")
    
    # Verify 阶段
    if result.get("verify_final_status") == "error":
        should_fail = True
        fail_reasons.append("verify final_status=error")
    
    if result.get("category_mismatch_A", 0) > 0:
        should_fail = True
        fail_reasons.append(f"verify Category A mismatch: {result['category_mismatch_A']}")
    
    if result.get("category_mismatch_B", 0) > 0:
        should_warn = True
        warn_reasons.append(f"verify Category B mismatch: {result['category_mismatch_B']}")
    
    # Overall status
    if result.get("overall_status") == "error":
        should_fail = True
        if "overall_status=error" not in fail_reasons:
            fail_reasons.append("overall_status=error")
    
    # 生成摘要
    summary_parts = []
    if should_fail:
        summary_parts.append(f"FAIL: {'; '.join(fail_reasons)}")
    if should_warn:
        summary_parts.append(f"WARN: {'; '.join(warn_reasons)}")
    if not should_fail and not should_warn:
        summary_parts.append("OK: All checks passed")
    
    return {
        "should_fail": should_fail,
        "should_warn": should_warn,
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "summary": " | ".join(summary_parts),
    }


def main():
    # 解析参数
    check_path = None
    apply_path = None
    verify_path = None
    unified_source = None  # 单一 JSON 源（文件或 "-" 表示 stdin）
    github_output_file = None
    use_unified_mode = False
    
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--check" and i + 1 < len(args):
            check_path = args[i + 1]
            i += 2
        elif args[i] == "--apply" and i + 1 < len(args):
            apply_path = args[i + 1]
            i += 2
        elif args[i] == "--verify" and i + 1 < len(args):
            verify_path = args[i + 1]
            i += 2
        elif args[i] == "--github-output" and i + 1 < len(args):
            github_output_file = args[i + 1]
            i += 2
        elif args[i] in ("--help", "-h"):
            print(__doc__)
            return 0
        elif args[i] == "-" or not args[i].startswith("-"):
            # 单参数模式：统一 JSON 源（"-" 表示 stdin）
            unified_source = args[i]
            use_unified_mode = True
            i += 1
        else:
            i += 1
    
    # 也支持 GITHUB_OUTPUT 环境变量
    if not github_output_file:
        github_output_file = os.environ.get("GITHUB_OUTPUT")
    
    # 判断模式
    if unified_source == "-" or (unified_source and not check_path and not apply_path and not verify_path):
        # 统一报告模式：从单一 JSON 读取
        use_unified_mode = True
    elif check_path or apply_path or verify_path:
        # 多文件模式
        use_unified_mode = False
    else:
        # 默认多文件模式（兼容原有行为）
        use_unified_mode = False
    
    if use_unified_mode:
        # 统一报告模式
        source = unified_source or "-"
        print(f"=== OpenMemory Sync Parse (Unified Mode) ===", file=sys.stderr)
        print(f"Source: {source}", file=sys.stderr)
        print("", file=sys.stderr)
        
        try:
            data = read_json_source(source)
            result = parse_unified_report(data)
        except FileNotFoundError:
            result = {"parse_error": f"Report file not found: {source}"}
        except json.JSONDecodeError as e:
            result = {"parse_error": f"Invalid JSON: {e}"}
        except Exception as e:
            result = {"parse_error": f"Unexpected error: {e}"}
        
        # 确定动作
        actions = determine_actions_unified(result)
        
        # 输出到 GITHUB_OUTPUT
        output_content = format_github_output_unified(result, actions)
        
        if github_output_file:
            with open(github_output_file, "a", encoding="utf-8") as f:
                f.write(output_content + "\n")
            print(f"[INFO] Written to GITHUB_OUTPUT: {github_output_file}", file=sys.stderr)
        
        # 输出 JSON 到 stdout
        output_json = {k: v for k, v in result.items() if not (k == "parse_error" and v is None)}
        output_json["should_fail"] = actions["should_fail"]
        output_json["should_warn"] = actions["should_warn"]
        output_json["summary"] = actions["summary"]
        print(json.dumps(output_json, indent=2, ensure_ascii=False))
        
        # 如果有解析错误，输出警告
        if result.get("parse_error"):
            print(f"\n[WARN] Parse error: {result['parse_error']}", file=sys.stderr)
            return 1
        
        return 0
    
    # 多文件模式
    # 默认路径
    if not check_path:
        check_path = ".artifacts/openmemory-sync-check.json"
    if not apply_path:
        apply_path = ".artifacts/openmemory-sync-apply.json"
    if not verify_path:
        verify_path = ".artifacts/openmemory-sync-verify.json"
    
    # 解析报告
    print("=== OpenMemory Sync Parse ===")
    print(f"Check:  {check_path}")
    print(f"Apply:  {apply_path}")
    print(f"Verify: {verify_path}")
    print("")
    
    check_result = parse_check_report(check_path)
    apply_result = parse_apply_report(apply_path)
    verify_result = parse_verify_report(verify_path)
    
    # 确定动作
    actions = determine_actions(verify_result, apply_result, check_result)
    
    # 输出到 GITHUB_OUTPUT
    output_content = format_github_output(verify_result, apply_result, check_result, actions)
    
    if github_output_file:
        with open(github_output_file, "a", encoding="utf-8") as f:
            f.write(output_content + "\n")
        print(f"[INFO] Written to GITHUB_OUTPUT: {github_output_file}")
    
    # 输出到 stdout
    print("")
    print("=== Parse Result ===")
    for line in output_content.split("\n"):
        print(f"  {line}")
    
    # 输出 Summary (如果在 GitHub Actions 环境)
    github_step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_step_summary:
        summary_md = generate_summary_markdown(verify_result, apply_result, check_result, actions)
        with open(github_step_summary, "a", encoding="utf-8") as f:
            f.write(summary_md + "\n\n")
        print(f"[INFO] Written to GITHUB_STEP_SUMMARY: {github_step_summary}")
    
    # 输出解析错误警告
    for name, result in [("check", check_result), ("apply", apply_result), ("verify", verify_result)]:
        if result.get("parse_error"):
            print(f"\n[WARN] {name} parse error: {result['parse_error']}", file=sys.stderr)
    
    # 返回 0（脚本总是成功，fail/warn 通过输出字段判断）
    return 0


if __name__ == "__main__":
    sys.exit(main())
