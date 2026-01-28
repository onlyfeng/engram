#!/usr/bin/env python3
"""
OpenMemory Drift Report 解析辅助脚本

统一处理 openmemory-upstream-drift.json 的解析，
替代 workflow 中分散的 grep/inline Python 解析逻辑。

用法:
    python scripts/openmemory_drift_parse.py [drift_json_path] [--github-output FILE]

输出 (GITHUB_OUTPUT 格式):
    priority=<security|high|normal|low|none|unknown>
    is_behind=<true|false>
    drift_level=<major|minor|patch|none|unknown>
    latest_ref=<version|N/A>
    is_frozen=<true|false>
    override_valid=<true|false>
    has_security_update=<true|false>
    needs_override=<true|false>
    should_block_upgrade=<true|false>
    drift_exit_code=<0|1|3>

Exit Code 语义 (与 openmemory_upstream_drift_check.py 一致):
    0: 无漂移或低/正常/高优先级（不阻断 CI）
    1: 安全更新检测到（非阻断，但触发通知/Issue）
    3: 冻结且无有效 override（可选阻断升级步骤）
"""

import json
import os
import sys
from pathlib import Path


def parse_drift_report(report_path: str) -> dict:
    """
    解析 drift report JSON 文件，返回统一结构。
    
    Returns:
        {
            "priority": str,
            "is_behind": bool,
            "drift_level": str,
            "latest_ref": str,
            "is_frozen": bool,
            "override_valid": bool,
            "has_security_update": bool,
            "needs_override": bool,
            "should_block_upgrade": bool,
            "drift_exit_code": int,
            "parse_error": str | None,
        }
    """
    result = {
        "priority": "unknown",
        "is_behind": False,
        "drift_level": "unknown",
        "latest_ref": "N/A",
        "is_frozen": False,
        "override_valid": False,
        "has_security_update": False,
        "needs_override": False,
        "should_block_upgrade": False,
        "drift_exit_code": 0,
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
    
    # 提取基本字段
    result["priority"] = data.get("priority", "none")
    result["is_behind"] = data.get("is_behind", False)
    result["drift_level"] = data.get("drift_level", "none")
    result["latest_ref"] = data.get("latest_ref") or "N/A"
    
    # 提取 freeze 状态
    freeze_status = data.get("freeze_status", {})
    result["is_frozen"] = freeze_status.get("is_frozen", False)
    result["override_valid"] = freeze_status.get("override_valid", False)
    override_requested = freeze_status.get("override_requested", False)
    
    # 派生字段：安全更新
    result["has_security_update"] = result["priority"] == "security"
    
    # 派生字段：需要 override
    # 条件：冻结状态 且 无有效 override
    result["needs_override"] = result["is_frozen"] and not result["override_valid"]
    
    # 派生字段：应该阻断升级步骤
    # 条件：请求了 override 但 override 无效
    # 注意：只有在显式请求 override 但无效时才阻断（即使是 security 也会被阻断）
    # 如果根本没有请求 override，即使冻结也不阻断（只是警告）
    result["should_block_upgrade"] = (
        override_requested and 
        not result["override_valid"]
    )
    
    # 推断 exit code（用于 CI 判断）
    # 与 openmemory_upstream_drift_check.py 的 exit 逻辑一致：
    # 1. 只有在 override_requested=True 且 override_valid=False 时才 exit 3
    # 2. security priority 时 exit 1
    # 3. 其他情况 exit 0
    if override_requested and not result["override_valid"]:
        # 请求了 override 但无效 - 阻断
        result["drift_exit_code"] = 3
    elif result["has_security_update"]:
        # 安全更新 - 非阻断但触发通知
        result["drift_exit_code"] = 1
    else:
        result["drift_exit_code"] = 0
    
    return result


def format_github_output(result: dict) -> str:
    """格式化为 GITHUB_OUTPUT 格式。"""
    lines = []
    for key, value in result.items():
        if key == "parse_error" and value is None:
            continue
        # 布尔值转小写字符串
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"{key}={value}")
    return "\n".join(lines)


def main():
    # 默认路径
    default_path = ".artifacts/openmemory-upstream-drift.json"
    
    # 解析参数
    report_path = default_path
    github_output_file = None
    
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--github-output" and i + 1 < len(args):
            github_output_file = args[i + 1]
            i += 2
        elif not args[i].startswith("-"):
            report_path = args[i]
            i += 1
        else:
            i += 1
    
    # 也支持 GITHUB_OUTPUT 环境变量
    if not github_output_file:
        github_output_file = os.environ.get("GITHUB_OUTPUT")
    
    # 解析报告
    result = parse_drift_report(report_path)
    
    # 输出到 GITHUB_OUTPUT（如果指定）
    output_content = format_github_output(result)
    
    if github_output_file:
        with open(github_output_file, "a", encoding="utf-8") as f:
            f.write(output_content + "\n")
        print(f"[INFO] Written to GITHUB_OUTPUT: {github_output_file}")
    
    # 同时输出到 stdout（便于调试）
    print("=== Drift Report Parse Result ===")
    for line in output_content.split("\n"):
        print(f"  {line}")
    
    # 如果有解析错误，输出警告
    if result["parse_error"]:
        print(f"\n[WARN] Parse error: {result['parse_error']}", file=sys.stderr)
    
    # 返回推断的 exit code（0 表示正常解析）
    # 注意：这个脚本本身总是返回 0，drift_exit_code 是数据字段
    return 0


if __name__ == "__main__":
    sys.exit(main())
