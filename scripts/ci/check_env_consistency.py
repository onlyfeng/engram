#!/usr/bin/env python3
"""
静态检查脚本：扫描 workflow + Makefile 中关键环境变量的矛盾配置

检查项：
1. CI/Nightly workflow 与 Makefile acceptance targets 的环境变量一致性
2. HTTP_ONLY_MODE / SKIP_DEGRADATION_TEST / VERIFY_FULL 的语义绑定
3. SEEKDB_ENABLE 传递的正确性

使用方式：
    python scripts/ci/check_env_consistency.py [--strict] [--json]

退出码：
    0 - 检查通过（无 error）
    1 - 存在 error 级别问题
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# ==============================================================================
# 环境变量基线定义（单一来源）
# ==============================================================================
# 这些值必须与 docs/ci_nightly_workflow_refactor/contract.md 保持一致

ENV_BASELINE = {
    "ci_standard": {
        "profile": "http_only",
        "HTTP_ONLY_MODE": "1",
        "SKIP_DEGRADATION_TEST": "1",
        "VERIFY_FULL": None,  # 不设置
        "SEEKDB_ENABLE": "1",
        "GATE_PROFILE": "http_only",
    },
    "nightly_full": {
        "profile": "full",
        "HTTP_ONLY_MODE": "0",
        "SKIP_DEGRADATION_TEST": "0",
        "VERIFY_FULL": "1",
        "SEEKDB_ENABLE": "1",
        "GATE_PROFILE": "full",
    },
}

# Makefile 目标与环境变量的预期绑定
MAKEFILE_TARGETS = {
    "acceptance-unified-min": {
        "HTTP_ONLY_MODE": "1",
        "SKIP_DEGRADATION_TEST": "1",
        "VERIFY_FULL": None,
        "GATE_PROFILE": "http_only",
        "SEEKDB_ENABLE": "$(SEEKDB_ENABLE_EFFECTIVE)",
    },
    "acceptance-unified-full": {
        "HTTP_ONLY_MODE": "0",
        "SKIP_DEGRADATION_TEST": "0",
        "VERIFY_FULL": "1",
        "GATE_PROFILE": "full",
        "SEEKDB_ENABLE": "$(SEEKDB_ENABLE_EFFECTIVE)",
    },
}


def parse_workflow_env(workflow_path: Path) -> dict[str, dict[str, str]]:
    """解析 workflow 文件中的环境变量设置"""
    results = {}
    
    if not workflow_path.exists():
        return results
    
    content = workflow_path.read_text()
    
    # 解析全局 env 块（workflow 顶层）
    global_env = {}
    global_env_match = re.search(
        r'^env:\s*\n((?:\s+#.*\n|\s+[A-Z_]+:.*\n)+)',
        content,
        re.MULTILINE
    )
    if global_env_match:
        env_block = global_env_match.group(1)
        for line in env_block.strip().split('\n'):
            if line.strip().startswith('#'):
                continue
            if ':' in line:
                parts = line.strip().split(':', 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().strip('"').strip("'")
                    if key and key[0].isupper() and val:
                        global_env[key] = val
    
    # 解析 job 级别的 env 块
    # 简化解析：查找 env: 块后的 key: value 对
    
    # 查找 unified-standard job 的 env
    ci_env_match = re.search(
        r'unified-standard:.*?env:\s*\n((?:\s+[A-Z_]+:.*\n)+)',
        content,
        re.DOTALL
    )
    if ci_env_match:
        env_block = ci_env_match.group(1)
        results["ci_standard"] = dict(global_env)  # 继承全局 env
        for line in env_block.strip().split('\n'):
            if ':' in line and not line.strip().startswith('#'):
                key, val = line.strip().split(':', 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # 处理 matrix 变量引用
                if '${{' in val:
                    # 提取 matrix 值
                    matrix_match = re.search(r'\$\{\{\s*matrix\.(\w+)\s*\}\}', val)
                    if matrix_match:
                        results["ci_standard"][key] = f"${{matrix.{matrix_match.group(1)}}}"
                    else:
                        results["ci_standard"][key] = val
                else:
                    results["ci_standard"][key] = val
    
    # 查找 nightly-full job 的 env（在 nightly.yml 中）
    # nightly.yml 中 nightly-full job 的 env 块在 job 定义下
    nightly_env_match = re.search(
        r'nightly-full:\s*\n.*?env:\s*\n((?:\s+#.*\n|\s+[A-Z_]+:.*\n)+)',
        content,
        re.DOTALL
    )
    if nightly_env_match:
        env_block = nightly_env_match.group(1)
        results["nightly_full"] = dict(global_env)  # 继承全局 env
        for line in env_block.strip().split('\n'):
            # 跳过注释行
            if line.strip().startswith('#'):
                continue
            if ':' in line:
                parts = line.strip().split(':', 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().strip('"').strip("'")
                    # 跳过空值或仅注释
                    if key and key[0].isupper() and val:
                        results["nightly_full"][key] = val
    elif global_env:
        # 如果没有 job 级别的 env，但有全局 env，使用全局 env
        results["nightly_full"] = dict(global_env)
    
    return results


def parse_makefile_target(makefile_path: Path, target_name: str) -> dict[str, str]:
    """解析 Makefile 中指定 target 的环境变量设置"""
    results = {}
    
    if not makefile_path.exists():
        return results
    
    content = makefile_path.read_text()
    
    # 查找目标定义及其调用的子目标时传递的环境变量
    # 模式：VAR=value $(MAKE) subtarget
    target_pattern = rf'^{re.escape(target_name)}:.*?(?=^\w+:|^#\s*===|\Z)'
    target_match = re.search(target_pattern, content, re.MULTILINE | re.DOTALL)
    
    if target_match:
        target_content = target_match.group(0)
        
        # 查找 export VAR=value; 形式
        for match in re.finditer(r'export\s+([A-Z_]+)=([^;\s]+)', target_content):
            results[match.group(1)] = match.group(2)
        
        # 查找 VAR=value $(MAKE) 调用（更精确的模式）
        # 匹配 VAR=value 后跟空格和 $(MAKE) 或 SEEKDB_ENABLE_EFFECTIVE
        for match in re.finditer(r'\b([A-Z_]+)=([^\s;]+)\s+(?:\$\(MAKE\)|SEEKDB_ENABLE)', target_content):
            val = match.group(2).rstrip(';')
            results[match.group(1)] = val
        
        # 查找连续的 VAR=value VAR2=value2 $(MAKE) 形式
        # 这在 Makefile 的 if ... then 块中很常见
        env_chain_pattern = r'if\s+((?:[A-Z_]+=\S+\s+)+)\$\(MAKE\)'
        for match in re.finditer(env_chain_pattern, target_content):
            env_pairs = match.group(1)
            for pair_match in re.finditer(r'([A-Z_]+)=([^\s;]+)', env_pairs):
                val = pair_match.group(2).rstrip(';')
                results[pair_match.group(1)] = val
    
    return results


def check_consistency(
    workflow_env: dict[str, dict[str, str]],
    makefile_env: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """检查环境变量一致性"""
    issues = []
    
    # 检查 CI Standard 层
    if "ci_standard" in workflow_env:
        ci_env = workflow_env["ci_standard"]
        expected = ENV_BASELINE["ci_standard"]
        
        for key, expected_val in expected.items():
            if key == "profile":
                continue
            actual_val = ci_env.get(key)
            
            # 处理 matrix 变量
            if actual_val and actual_val.startswith("${"):
                continue  # matrix 变量需要额外解析，暂时跳过
            
            if expected_val is not None and actual_val != expected_val:
                issues.append({
                    "level": "error" if key in ("HTTP_ONLY_MODE", "SKIP_DEGRADATION_TEST") else "warning",
                    "layer": "ci_standard",
                    "variable": key,
                    "expected": expected_val,
                    "actual": actual_val,
                    "message": f"CI Standard 层 {key} 值不一致: 预期 '{expected_val}', 实际 '{actual_val}'",
                })
    
    # 检查 Nightly Full 层
    if "nightly_full" in workflow_env:
        nightly_env = workflow_env["nightly_full"]
        expected = ENV_BASELINE["nightly_full"]
        
        for key, expected_val in expected.items():
            if key == "profile":
                continue
            actual_val = nightly_env.get(key)
            
            if expected_val is not None and actual_val != expected_val:
                issues.append({
                    "level": "error" if key in ("VERIFY_FULL", "HTTP_ONLY_MODE", "SKIP_DEGRADATION_TEST") else "warning",
                    "layer": "nightly_full",
                    "variable": key,
                    "expected": expected_val,
                    "actual": actual_val,
                    "message": f"Nightly Full 层 {key} 值不一致: 预期 '{expected_val}', 实际 '{actual_val}'",
                })
    
    # 检查 Makefile targets
    for target_name, expected_env in MAKEFILE_TARGETS.items():
        if target_name in makefile_env:
            actual_env = makefile_env[target_name]
            
            for key, expected_val in expected_env.items():
                actual_val = actual_env.get(key)
                
                # SEEKDB_ENABLE 需要检查是否使用了 $(SEEKDB_ENABLE_EFFECTIVE)
                if key == "SEEKDB_ENABLE":
                    if actual_val and "SEEKDB_ENABLE_EFFECTIVE" not in actual_val:
                        issues.append({
                            "level": "warning",
                            "layer": f"makefile:{target_name}",
                            "variable": key,
                            "expected": expected_val,
                            "actual": actual_val,
                            "message": f"Makefile {target_name} 中 {key} 应使用 $(SEEKDB_ENABLE_EFFECTIVE)",
                        })
                elif expected_val is not None and actual_val != expected_val:
                    issues.append({
                        "level": "error",
                        "layer": f"makefile:{target_name}",
                        "variable": key,
                        "expected": expected_val,
                        "actual": actual_val,
                        "message": f"Makefile {target_name} 中 {key} 值不一致: 预期 '{expected_val}', 实际 '{actual_val}'",
                    })
    
    return issues


def main():
    parser = argparse.ArgumentParser(
        description="检查 workflow 和 Makefile 中环境变量的一致性"
    )
    parser.add_argument("--strict", action="store_true", help="严格模式：warning 视为 error")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--ci-yml", default=".github/workflows/ci.yml", help="CI workflow 路径")
    parser.add_argument("--nightly-yml", default=".github/workflows/nightly.yml", help="Nightly workflow 路径")
    parser.add_argument("--makefile", default="Makefile", help="Makefile 路径")
    args = parser.parse_args()
    
    # 确定项目根目录
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent
    
    ci_yml = project_root / args.ci_yml
    nightly_yml = project_root / args.nightly_yml
    makefile = project_root / args.makefile
    
    # 解析文件
    workflow_env = {}
    
    ci_env = parse_workflow_env(ci_yml)
    if ci_env:
        workflow_env.update(ci_env)
    
    nightly_env = parse_workflow_env(nightly_yml)
    if nightly_env:
        workflow_env.update(nightly_env)
    
    makefile_env = {}
    for target in MAKEFILE_TARGETS:
        target_env = parse_makefile_target(makefile, target)
        if target_env:
            makefile_env[target] = target_env
    
    # 检查一致性
    issues = check_consistency(workflow_env, makefile_env)
    
    # 输出结果
    has_errors = any(i["level"] == "error" for i in issues)
    has_warnings = any(i["level"] == "warning" for i in issues)
    
    if args.json:
        result = {
            "ok": not has_errors and (not args.strict or not has_warnings),
            "error_count": sum(1 for i in issues if i["level"] == "error"),
            "warning_count": sum(1 for i in issues if i["level"] == "warning"),
            "issues": issues,
            "baseline": ENV_BASELINE,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("=" * 60)
        print("环境变量一致性检查")
        print("=" * 60)
        print()
        
        if not issues:
            print("[OK] 所有检查通过")
        else:
            for issue in issues:
                level_marker = "[ERROR]" if issue["level"] == "error" else "[WARN]"
                print(f"{level_marker} {issue['message']}")
                print(f"    位置: {issue['layer']}")
                print()
        
        print("-" * 60)
        print(f"错误: {sum(1 for i in issues if i['level'] == 'error')}")
        print(f"警告: {sum(1 for i in issues if i['level'] == 'warning')}")
        print()
        
        if has_errors:
            print("[FAIL] 存在 error 级别问题")
        elif args.strict and has_warnings:
            print("[FAIL] 严格模式：存在 warning 级别问题")
        else:
            print("[OK] 检查通过")
    
    # 退出码
    if has_errors:
        sys.exit(1)
    if args.strict and has_warnings:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
