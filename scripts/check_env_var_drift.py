#!/usr/bin/env python3
"""
环境变量漂移检查脚本

检测代码中直接使用已废弃环境变量名的情况。
已废弃变量应通过 env_compat 兼容层访问，不应在新代码中直接引用。

用法:
    python scripts/check_env_var_drift.py [--fail]
    
选项:
    --fail    发现问题时返回非零退出码（默认 warning 模式仅输出警告）

退出码:
    0: 无问题或 warning 模式
    1: fail 模式下发现问题
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Pattern, Set, Tuple

# ============================================================================
# 配置区: deprecated 变量名清单与允许出现的位置
# ============================================================================

# 已废弃的环境变量名 -> canonical 名称映射
DEPRECATED_ENV_VARS: dict[str, str] = {
    # Step3 PGVector 配置
    "STEP3_SCHEMA": "STEP3_PG_SCHEMA",
    "STEP3_TABLE": "STEP3_PG_TABLE",
    "STEP3_AUTO_INIT": "STEP3_PG_AUTO_INIT",
    "PG_HOST": "STEP3_PG_HOST (或 PGHOST)",
    "POSTGRES_HOST": "STEP3_PG_HOST (或 PGHOST)",
    # Step3 Dual Write
    "DUAL_WRITE_ENABLED": "STEP3_DUAL_WRITE",
}

# 允许出现 deprecated 变量的文件/目录模式（相对于项目根目录）
# 这些位置通常是兼容层、测试、或历史文档
ALLOWED_PATHS: list[str] = [
    # ========== 兼容层模块 ==========
    "apps/step3_seekdb_rag_hybrid/env_compat.py",
    "apps/step3_seekdb_rag_hybrid/tests/test_env_compat.py",
    # Step3 后端工厂（实现多优先级环境变量回退）
    "apps/step3_seekdb_rag_hybrid/step3_backend_factory.py",
    # 迁移脚本中的兼容逻辑（需要处理旧变量）
    "apps/step3_seekdb_rag_hybrid/scripts/pgvector_collection_migrate.py",
    "apps/step3_seekdb_rag_hybrid/scripts/pgvector_inspect.py",
    # 数据库操作脚本（实现兼容层检测与回退）
    "scripts/db_ops.sh",
    # ========== CI/测试脚本 ==========
    # Step1 CI 测试脚本（使用标准 PG_* 变量）
    "apps/step1_logbook_postgres/scripts/ci/",
    # PGVector 集成测试（需要直接配置数据库连接）
    "apps/step3_seekdb_rag_hybrid/tests/test_pgvector_backend_integration.py",
    # ========== 文档 ==========
    # Step3 文档（说明废弃别名）
    "apps/step3_seekdb_rag_hybrid/docs/",
    # 历史文档（迁移完成后应移除）
    "docs/legacy/",
    # ========== 本脚本 ==========
    "scripts/check_env_var_drift.py",
]

# 要扫描的文件后缀
SCAN_EXTENSIONS: set[str] = {".py", ".sh", ".yml", ".yaml", ".md"}

# 忽略的目录
IGNORE_DIRS: set[str] = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".eggs",
    "*.egg-info",
    "archives",
    "libs/OpenMemory",  # 上游依赖，不检查
}


# ============================================================================
# 检测逻辑
# ============================================================================

@dataclass
class Violation:
    """检测到的违规项"""
    file_path: str
    line_number: int
    line_content: str
    deprecated_var: str
    canonical_var: str


@dataclass
class CheckResult:
    """检查结果"""
    violations: List[Violation] = field(default_factory=list)
    scanned_files: int = 0
    

def _is_path_allowed(file_path: str, project_root: Path) -> bool:
    """检查文件路径是否在允许列表中"""
    rel_path = str(Path(file_path).relative_to(project_root))
    for allowed in ALLOWED_PATHS:
        if allowed.endswith("/"):
            # 目录匹配
            if rel_path.startswith(allowed):
                return True
        else:
            # 文件精确匹配
            if rel_path == allowed:
                return True
    return False


def _should_ignore_dir(dir_name: str) -> bool:
    """检查目录是否应该被忽略"""
    for pattern in IGNORE_DIRS:
        if pattern.endswith("*"):
            if dir_name.startswith(pattern[:-1]):
                return True
        elif dir_name == pattern:
            return True
    return False


def _build_patterns() -> dict[str, Tuple[Pattern, str]]:
    """构建搜索模式"""
    patterns = {}
    for deprecated, canonical in DEPRECATED_ENV_VARS.items():
        # 匹配环境变量引用的常见模式：
        # - os.environ["VAR"] / os.environ.get("VAR") / os.getenv("VAR")
        # - ${VAR} / $VAR (shell/yaml)
        # - deprecated_aliases=["VAR"] (兼容层定义，允许)
        # - 字符串中的 "VAR" 引用
        pattern = re.compile(
            rf'\b{re.escape(deprecated)}\b',
            re.IGNORECASE
        )
        patterns[deprecated] = (pattern, canonical)
    return patterns


def _is_in_comment(line: str, match_start: int) -> bool:
    """检查匹配是否在注释中"""
    # Python 单行注释
    hash_pos = line.find("#")
    if hash_pos != -1 and hash_pos < match_start:
        return True
    return False


def _is_deprecated_alias_definition(line: str) -> bool:
    """检查是否是 deprecated_aliases 定义（兼容层中的合法使用）"""
    return "deprecated_aliases" in line or "deprecated_value_aliases" in line


def scan_file(
    file_path: Path,
    project_root: Path,
    patterns: dict[str, Tuple[Pattern, str]]
) -> List[Violation]:
    """扫描单个文件"""
    violations = []
    
    # 检查是否在允许列表
    if _is_path_allowed(str(file_path), project_root):
        return violations
    
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return violations
    
    for line_num, line in enumerate(content.splitlines(), start=1):
        # 跳过 deprecated_aliases 定义行
        if _is_deprecated_alias_definition(line):
            continue
            
        for deprecated, (pattern, canonical) in patterns.items():
            matches = list(pattern.finditer(line))
            for match in matches:
                # 跳过注释中的引用（文档说明）
                if _is_in_comment(line, match.start()):
                    continue
                    
                violations.append(Violation(
                    file_path=str(file_path.relative_to(project_root)),
                    line_number=line_num,
                    line_content=line.strip(),
                    deprecated_var=deprecated,
                    canonical_var=canonical,
                ))
    
    return violations


def scan_project(project_root: Path) -> CheckResult:
    """扫描整个项目"""
    result = CheckResult()
    patterns = _build_patterns()
    
    for root, dirs, files in os.walk(project_root):
        # 过滤忽略目录
        dirs[:] = [d for d in dirs if not _should_ignore_dir(d)]
        
        # 检查是否整个目录在忽略列表
        rel_root = Path(root).relative_to(project_root)
        should_skip = False
        for ignore in IGNORE_DIRS:
            if not ignore.endswith("*") and str(rel_root).startswith(ignore):
                should_skip = True
                break
        if should_skip:
            dirs.clear()
            continue
        
        for file_name in files:
            file_path = Path(root) / file_name
            
            # 检查文件后缀
            if file_path.suffix.lower() not in SCAN_EXTENSIONS:
                continue
            
            result.scanned_files += 1
            violations = scan_file(file_path, project_root, patterns)
            result.violations.extend(violations)
    
    return result


def print_report(result: CheckResult, fail_mode: bool) -> None:
    """打印检查报告"""
    prefix = "[ERROR]" if fail_mode else "[WARNING]"
    
    print(f"\n{'='*70}")
    print("环境变量漂移检查报告")
    print(f"{'='*70}")
    print(f"扫描文件数: {result.scanned_files}")
    print(f"发现问题数: {len(result.violations)}")
    
    if not result.violations:
        print("\n✓ 未发现直接使用废弃环境变量的情况")
        return
    
    print(f"\n{prefix} 发现以下文件直接使用了废弃环境变量:\n")
    
    # 按文件分组
    by_file: dict[str, List[Violation]] = {}
    for v in result.violations:
        by_file.setdefault(v.file_path, []).append(v)
    
    for file_path, violations in sorted(by_file.items()):
        print(f"  {file_path}:")
        for v in violations:
            print(f"    L{v.line_number}: {v.deprecated_var} -> 建议使用 {v.canonical_var}")
            print(f"         {v.line_content[:80]}...")
        print()
    
    print("建议:")
    print("  1. 使用 env_compat 模块的 get_str/get_int/get_bool 函数读取环境变量")
    print("  2. 在 deprecated_aliases 参数中声明废弃别名以保持兼容")
    print("  3. 如果此位置需要保留废弃变量引用，请添加到 ALLOWED_PATHS 列表")
    print()


def main() -> int:
    """主函数"""
    parser = argparse.ArgumentParser(
        description="检测代码中直接使用已废弃环境变量名的情况"
    )
    parser.add_argument(
        "--fail",
        action="store_true",
        help="发现问题时返回非零退出码（默认 warning 模式）"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认自动检测）"
    )
    args = parser.parse_args()
    
    # 确定项目根目录
    if args.project_root:
        project_root = args.project_root.resolve()
    else:
        # 从脚本位置向上查找
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent  # scripts/ 的父目录
    
    if not project_root.exists():
        print(f"[ERROR] 项目根目录不存在: {project_root}", file=sys.stderr)
        return 1
    
    print(f"项目根目录: {project_root}")
    print(f"检查模式: {'fail' if args.fail else 'warning'}")
    
    # 执行扫描
    result = scan_project(project_root)
    
    # 打印报告
    print_report(result, args.fail)
    
    # 返回退出码
    if args.fail and result.violations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
