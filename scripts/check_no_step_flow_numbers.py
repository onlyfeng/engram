#!/usr/bin/env python3
"""
检查仓库中是否存在旧版流程编号模式（Step N 形式）。

本脚本用于检测代码中残留的 Step 1/2/3 流程编号写法。
- 匹配：Step 后跟空格再跟数字 1/2/3（如 "Step 1", "Step 2", "step 3"）
- 应使用语义化阶段名称替代：logbook, gateway, seekdb

注意：此检查与 check_no_legacy_stage_aliases.py 互补：
- check_no_legacy_stage_aliases.py 检测无空格的旧别名（如 step1, step2_xxx）
- 本脚本检测带空格的流程编号写法（如 Step 1, step 2）

治理范围边界（参考 docs/architecture/adr_step_flow_wording.md §4.1）:
- IN SCOPE: docs/, scripts/, apps/, .github/
- OUT OF SCOPE: libs/OpenMemory/, patches/, archives/, vendor/, third_party/

用法:
    python scripts/check_no_step_flow_numbers.py [--verbose] [--json]
    python scripts/check_no_step_flow_numbers.py --fail    # 严格失败模式（CI 默认）
    python scripts/check_no_step_flow_numbers.py --no-fail # 仅警告，不失败

选项:
    --fail       发现问题时严格失败（CI hard gate，默认行为）
    --no-fail    发现问题时仅警告，不失败
    --verbose    输出详细信息
    --json       以 JSON 格式输出结果
    --root       指定项目根目录

退出码:
    0: 无问题或 --no-fail 模式
    1: 默认/--fail 模式下发现问题
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set

# ============================================================================
# 配置: 需要检测的标签模式
# ============================================================================

# 检测模式：Step 后跟空白字符再跟数字 1/2/3（大小写不敏感）
# 使用单词边界确保精确匹配
# 匹配示例：Step 1, Step 2, step 3, STEP 1
# 排除：step1（无空格）由另一个检查脚本处理
STEP_FLOW_PATTERN = re.compile(
    r'\bstep\s+[1-3]\b',  # word boundary + step + whitespace + 1/2/3 + word boundary
    re.IGNORECASE
)

# ============================================================================
# 治理范围边界（Governance Scope）
# 参考：docs/architecture/adr_step_flow_wording.md §4.1
# ============================================================================
#
# IN SCOPE（受治理）:
#   - docs/        项目文档
#   - scripts/     脚本和工具
#   - apps/        应用代码（排除 vendored 子目录）
#   - .github/     CI/CD 配置
#
# OUT OF SCOPE（不受治理）:
#   - libs/OpenMemory/  上游依赖快照（由 OpenMemory.upstream.lock.json 锁定）
#   - patches/          补丁文件（需保持与上游对应的原始上下文）
#   - archives/         上游快照存档
#   - vendor/           第三方 vendored 代码
#   - third_party/      第三方代码
#
# ============================================================================

# 排除的目录（相对于项目根目录）
# 与 ADR §4.1.2 边界定义保持一致
EXCLUDE_DIRS: Set[str] = {
    # === 版本控制和 CI 生成物 ===
    ".git",
    ".artifacts",       # CI 生成的临时制品
    
    # === 上游依赖和快照（ADR §4.1.2 OUT OF SCOPE）===
    "libs",             # 包含 OpenMemory 等外部依赖快照
    "patches",          # 补丁目录，需保持原始上下文
    "archives",         # 上游快照存档
    
    # === 第三方 vendored 代码 ===
    "vendor",
    "third_party",
    "node_modules",
    
    # === 构建和缓存目录 ===
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "dist",
    "build",
    ".eggs",
    "*.egg-info",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

# 排除的文件名模式
EXCLUDE_FILES: Set[str] = {
    "*.lock",
    "*.lock.json",
    "package-lock.json",
    "poetry.lock",
    "Pipfile.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.pyc",
    "*.pyo",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.bin",
    "*.wasm",
    "*.ico",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.svg",
    "*.webp",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    "*.pdf",
    "*.zip",
    "*.tar",
    "*.gz",
    "*.bz2",
    "*.xz",
}

# 允许出现流程编号的路径（白名单，相对于项目根目录）
# 参考：docs/architecture/adr_step_flow_wording.md §7.1 豁免场景
#
# 最小集合：检查脚本自身 + 架构文档中用正则/占位符描述被禁模式
# 白名单原因记录：
#   - 检查脚本/测试：脚本需要定义检测模式，测试需要验证检测功能
#   - 架构文档：包含禁止词示例（使用正则/占位符表达）
#   - 治理文档：ADR 需要引用被禁模式以说明规则
ALLOWED_PATHS: List[str] = [
    # ========== 本脚本及相关检查脚本/测试（ADR §7.1 测试用例）==========
    "scripts/check_no_step_flow_numbers.py",
    "scripts/check_no_legacy_stage_aliases.py",  # 互补脚本，可能引用此模式
    "scripts/tests/test_legacy_alias_checks.py",
    "scripts/tests/test_step_flow_checks.py",
    "scripts/tests/test_step_flow_number_checks.py",
    # ========== 架构文档（ADR §7.1 治理文档本身）==========
    "docs/architecture/naming.md",
    "docs/architecture/adr_step_flow_wording.md",  # 本 ADR 需要引用被禁模式
    "docs/architecture/legacy_naming_governance.md",
    # ========== Git 目录 ==========
    ".git/",
]

# 要扫描的文件后缀
SCAN_EXTENSIONS: Set[str] = {
    ".py",
    ".sh",
    ".md",
    ".yml",
    ".yaml",
    ".json",
    ".sql",
    ".toml",
}

# 要扫描的特殊文件名（无后缀）
SCAN_FILENAMES: Set[str] = {
    "Makefile",
}


@dataclass
class Finding:
    """检测到的问题"""
    file: str
    line: int
    column: int
    match: str
    context: str = ""  # 匹配行的上下文

    def to_dict(self) -> dict:
        """转换为字典（用于 JSON 序列化）"""
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "match": self.match,
            "context": self.context,
        }

    def to_ci_format(self) -> str:
        """转换为 CI 友好的格式（file:line:column: message）"""
        return f"{self.file}:{self.line}:{self.column}: step flow number '{self.match}'"


@dataclass
class ScanResult:
    """扫描结果"""
    findings: List[Finding] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped: int = 0


def should_exclude_dir(dir_name: str) -> bool:
    """检查目录是否应该被排除"""
    for pattern in EXCLUDE_DIRS:
        if pattern.startswith("*"):
            if dir_name.endswith(pattern[1:]):
                return True
        elif dir_name == pattern:
            return True
    return False


def should_exclude_file(file_name: str) -> bool:
    """检查文件是否应该被排除"""
    for pattern in EXCLUDE_FILES:
        if pattern.startswith("*"):
            if file_name.endswith(pattern[1:]):
                return True
        elif file_name == pattern:
            return True
    return False


def should_scan_file(file_path: Path) -> bool:
    """检查文件是否应该被扫描"""
    name = file_path.name
    suffix = file_path.suffix

    # 检查文件名是否在扫描列表中
    if name in SCAN_FILENAMES:
        return True
    if suffix in SCAN_EXTENSIONS:
        return True

    return False


def is_allowed_path(file_path: str) -> bool:
    """检查文件是否在白名单中"""
    for allowed in ALLOWED_PATHS:
        if allowed.endswith("/"):
            if file_path.startswith(allowed):
                return True
        else:
            if file_path == allowed:
                return True
    return False


def scan_file(file_path: Path, root: Path) -> List[Finding]:
    """扫描单个文件"""
    findings = []
    rel_path = str(file_path.relative_to(root))

    # 检查白名单
    if is_allowed_path(rel_path):
        return findings

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return findings

    for line_num, line in enumerate(lines, start=1):
        for match in STEP_FLOW_PATTERN.finditer(line):
            findings.append(Finding(
                file=rel_path,
                line=line_num,
                column=match.start() + 1,
                match=match.group(),
                context=line.rstrip()[:200],  # 截取上下文
            ))

    return findings


def scan_directory(root: Path, verbose: bool = False) -> ScanResult:
    """扫描目录"""
    result = ScanResult()

    for dirpath, dirnames, filenames in os.walk(root):
        # 过滤排除的目录
        dirnames[:] = [d for d in dirnames if not should_exclude_dir(d)]

        for filename in filenames:
            if should_exclude_file(filename):
                result.files_skipped += 1
                continue

            file_path = Path(dirpath) / filename

            if not should_scan_file(file_path):
                result.files_skipped += 1
                continue

            result.files_scanned += 1

            if verbose:
                print(f"Scanning: {file_path.relative_to(root)}", file=sys.stderr)

            file_findings = scan_file(file_path, root)
            result.findings.extend(file_findings)

    return result


def print_findings(result: ScanResult, verbose: bool = False, fail_mode: bool = True):
    """打印检测结果（CI hard gate 格式）"""
    prefix = "error" if fail_mode else "warning"

    if not result.findings:
        print("=" * 60)
        print("[PASS] No step flow numbers detected")
        print("=" * 60)
        print(f"  Files scanned: {result.files_scanned}")
        print(f"  Files skipped: {result.files_skipped}")
        return

    print("=" * 60)
    print(f"[FAIL] Step flow numbers detected ({len(result.findings)} issues)")
    print("=" * 60)
    print()

    # CI 友好格式：file:line:column: message
    for f in result.findings:
        print(f"{prefix}: {f.to_ci_format()}")
        if verbose:
            print(f"  | {f.context}")

    print()
    print("=" * 60)
    print(f"Summary: {len(result.findings)} step flow number(s) in {len(set(f.file for f in result.findings))} file(s)")
    print(f"Files scanned: {result.files_scanned}, Files skipped: {result.files_skipped}")
    print("=" * 60)
    print()
    print("Hint: Replace step flow numbers (Step 1/2/3) with semantic stage names (logbook, gateway, seekdb).")


def print_json(result: ScanResult, fail_mode: bool = True):
    """以 JSON 格式输出结果"""
    has_issues = bool(result.findings)
    output = {
        "status": "error" if (has_issues and fail_mode) else ("warning" if has_issues else "ok"),
        "errors": len(result.findings),
        "files_scanned": result.files_scanned,
        "files_skipped": result.files_skipped,
        "findings": [f.to_dict() for f in result.findings],
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="检查仓库中是否存在旧版流程编号模式（CI hard gate）"
    )
    parser.add_argument(
        "--fail",
        action="store_true",
        default=True,
        help="发现问题时严格失败（默认行为，CI hard gate）",
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="发现问题时仅警告，不失败（覆盖 --fail）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="输出详细信息（包含匹配行上下文）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="项目根目录（默认为当前目录或 git 根目录）",
    )

    args = parser.parse_args()

    # --no-fail 覆盖 --fail
    fail_mode = not args.no_fail

    # 确定项目根目录
    if args.root:
        root = args.root.resolve()
    else:
        # 尝试找到项目根目录（包含 .git 或 Makefile）
        root = Path.cwd()
        while root != root.parent:
            if (root / ".git").exists() or (root / "Makefile").exists():
                break
            root = root.parent
        else:
            root = Path.cwd()

    # 打印配置信息（非 JSON 模式）
    if not args.json:
        print(f"Root: {root}")
        mode_desc = "strict (exit 1 on error)" if fail_mode else "warning only (exit 0)"
        print(f"Mode: {mode_desc}")
        print()

    # 扫描
    result = scan_directory(root, verbose=args.verbose)

    # 输出
    if args.json:
        print_json(result, fail_mode=fail_mode)
    else:
        print_findings(result, verbose=args.verbose, fail_mode=fail_mode)

    # 退出码：--fail 模式下发现问题则失败
    if fail_mode and result.findings:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
