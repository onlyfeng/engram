#!/usr/bin/env python3
"""
环境变量漂移检查脚本

检测代码中直接使用已废弃环境变量名的情况。
已废弃变量应通过 env_compat 兼容层访问，不应在新代码中直接引用。

用法:
    python scripts/check_env_var_drift.py [--fail] [--fail-on-new] [--strict]
    
选项:
    --fail         发现问题时返回非零退出码（默认 warning 模式仅输出警告）
    --fail-on-new  仅对新增违规（相对基线）fail，已知基线违规仅警告
    --strict       对非文档/非兼容层路径全面 fail（中期目标）
    --save-baseline FILE  保存当前违规作为基线到指定文件
    --baseline FILE       加载基线文件进行比较

环境变量:
    ENV_DRIFT_FAIL=1       等同于 --fail
    ENV_DRIFT_STRICT=1     等同于 --strict
    ENV_DRIFT_FAIL_ON_NEW=1 等同于 --fail-on-new

退出码:
    0: 无问题或 warning 模式
    1: fail 模式下发现问题
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Set, Tuple

# ============================================================================
# 配置区: deprecated 变量名清单与允许出现的位置
# ============================================================================

# 已废弃的环境变量名 -> canonical 名称映射
# Add deprecated environment variables here as they are identified
DEPRECATED_ENV_VARS: dict[str, str] = {
    # Example:
    # "OLD_VAR_NAME": "NEW_VAR_NAME",
}

# 允许出现 deprecated 变量的文件/目录模式（相对于项目根目录）
# 严格限制在：兼容层、测试、文档
ALLOWED_PATHS: list[str] = [
    # ========== 文档目录（说明废弃别名和迁移指南）==========
    "docs/reference/",
    "docs/legacy/",
    # ========== 本脚本（定义废弃变量列表）==========
    "scripts/check_env_var_drift.py",
]

# 文档和兼容层路径（strict 模式下仍然允许）
# 这些路径在 --strict 模式下也不会 fail
DOC_AND_COMPAT_PATHS: list[str] = [
    # 文档目录
    "docs/reference/",
    "docs/legacy/",
    # 本脚本
    "scripts/check_env_var_drift.py",
]

# 默认基线文件路径
DEFAULT_BASELINE_PATH = ".drift_baseline.json"

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

    def to_key(self) -> str:
        """生成唯一标识键（用于基线比较）"""
        return f"{self.file_path}:{self.line_number}:{self.deprecated_var}"

    def to_dict(self) -> dict:
        """转换为字典（用于 JSON 序列化）"""
        return {
            "file_path": self.file_path,
            "line_number": self.line_number,
            "line_content": self.line_content,
            "deprecated_var": self.deprecated_var,
            "canonical_var": self.canonical_var,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Violation":
        """从字典创建（用于 JSON 反序列化）"""
        return cls(
            file_path=data["file_path"],
            line_number=data["line_number"],
            line_content=data["line_content"],
            deprecated_var=data["deprecated_var"],
            canonical_var=data["canonical_var"],
        )


@dataclass
class CheckResult:
    """检查结果"""
    violations: List[Violation] = field(default_factory=list)
    scanned_files: int = 0

    def get_violation_keys(self) -> Set[str]:
        """获取所有违规的唯一键集合"""
        return {v.to_key() for v in self.violations}

    def to_baseline_dict(self) -> dict:
        """转换为基线字典"""
        return {
            "version": 1,
            "violations": [v.to_dict() for v in self.violations],
        }

    @classmethod
    def from_baseline_dict(cls, data: dict) -> "CheckResult":
        """从基线字典创建"""
        result = cls()
        for v_dict in data.get("violations", []):
            result.violations.append(Violation.from_dict(v_dict))
        return result


def _is_doc_or_compat_path(file_path: str, project_root: Path) -> bool:
    """检查文件是否在文档或兼容层路径中"""
    rel_path = str(Path(file_path).relative_to(project_root))
    for allowed in DOC_AND_COMPAT_PATHS:
        if allowed.endswith("/"):
            if rel_path.startswith(allowed):
                return True
        else:
            if rel_path == allowed:
                return True
    return False


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


def load_baseline(baseline_path: Path) -> Optional[CheckResult]:
    """加载基线文件"""
    if not baseline_path.exists():
        return None
    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return CheckResult.from_baseline_dict(data)
    except Exception as e:
        print(f"[WARNING] 无法加载基线文件 {baseline_path}: {e}", file=sys.stderr)
        return None


def save_baseline(result: CheckResult, baseline_path: Path) -> bool:
    """保存基线文件"""
    try:
        with open(baseline_path, "w", encoding="utf-8") as f:
            json.dump(result.to_baseline_dict(), f, indent=2, ensure_ascii=False)
        print(f"[INFO] 基线已保存到 {baseline_path}")
        return True
    except Exception as e:
        print(f"[ERROR] 无法保存基线文件 {baseline_path}: {e}", file=sys.stderr)
        return False


def classify_violations(
    result: CheckResult,
    project_root: Path,
    baseline: Optional[CheckResult] = None,
) -> Dict[str, List[Violation]]:
    """
    对违规进行分类
    
    返回:
        {
            "new": 新增违规（相对基线）,
            "baseline": 基线中已存在的违规,
            "strict": 非文档/兼容层路径的违规（中期需修复）,
            "doc_compat": 文档/兼容层路径的违规（可接受）,
        }
    """
    baseline_keys = baseline.get_violation_keys() if baseline else set()

    classified = {
        "new": [],
        "baseline": [],
        "strict": [],
        "doc_compat": [],
    }

    for v in result.violations:
        key = v.to_key()
        is_doc_compat = _is_doc_or_compat_path(v.file_path, project_root)

        # 分类: 新增 vs 基线已有
        if key in baseline_keys:
            classified["baseline"].append(v)
        else:
            classified["new"].append(v)

        # 分类: 文档/兼容层 vs 严格检查路径
        if is_doc_compat:
            classified["doc_compat"].append(v)
        else:
            classified["strict"].append(v)

    return classified


def print_classified_report(
    result: CheckResult,
    classified: Dict[str, List[Violation]],
    fail_mode: bool,
    strict_mode: bool,
    fail_on_new: bool,
) -> None:
    """打印分类检查报告"""
    print(f"\n{'='*70}")
    print("环境变量漂移检查报告")
    print(f"{'='*70}")
    print(f"扫描文件数: {result.scanned_files}")
    print(f"发现问题数: {len(result.violations)}")
    print(f"  - 新增违规: {len(classified['new'])}")
    print(f"  - 基线已有: {len(classified['baseline'])}")
    print(f"  - 严格检查路径: {len(classified['strict'])}")
    print(f"  - 文档/兼容层: {len(classified['doc_compat'])}")

    if not result.violations:
        print("\n✓ 未发现直接使用废弃环境变量的情况")
        return

    # 确定哪些类别会导致失败
    will_fail = False
    fail_reasons = []

    if fail_mode and result.violations:
        will_fail = True
        fail_reasons.append("--fail 模式，所有违规都会导致失败")

    if fail_on_new and classified["new"]:
        will_fail = True
        fail_reasons.append(f"--fail-on-new 模式，发现 {len(classified['new'])} 个新增违规")

    if strict_mode and classified["strict"]:
        # strict 模式下，非文档/兼容层路径的违规（不含已在基线中的）会导致失败
        strict_new = [v for v in classified["strict"] if v in classified["new"]]
        if strict_new:
            will_fail = True
            fail_reasons.append(f"--strict 模式，发现 {len(strict_new)} 个非文档路径的新增违规")

    prefix = "[ERROR]" if will_fail else "[WARNING]"

    # 打印新增违规
    if classified["new"]:
        print(f"\n{prefix} 新增违规（必须修复）:\n")
        _print_violations_by_file(classified["new"])

    # 打印基线违规（仅当 verbose 或 fail 模式）
    if classified["baseline"]:
        print("\n[INFO] 基线已有违规（待迁移）:\n")
        _print_violations_by_file(classified["baseline"])

    print("\n建议:")
    print("  1. 使用 env_compat 模块的 get_str/get_int/get_bool 函数读取环境变量")
    print("  2. 在 deprecated_aliases 参数中声明废弃别名以保持兼容")
    print("  3. 如果此位置需要保留废弃变量引用，请添加到 ALLOWED_PATHS 列表")
    if fail_reasons:
        print(f"\n失败原因: {'; '.join(fail_reasons)}")
    print()


def _print_violations_by_file(violations: List[Violation]) -> None:
    """按文件分组打印违规"""
    by_file: dict[str, List[Violation]] = {}
    for v in violations:
        by_file.setdefault(v.file_path, []).append(v)

    for file_path, file_violations in sorted(by_file.items()):
        print(f"  {file_path}:")
        for v in file_violations:
            print(f"    L{v.line_number}: {v.deprecated_var} -> 建议使用 {v.canonical_var}")
            content_preview = v.line_content[:80]
            if len(v.line_content) > 80:
                content_preview += "..."
            print(f"         {content_preview}")
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
        "--fail-on-new",
        action="store_true",
        help="仅对新增违规（相对基线）fail，基线已有违规仅警告"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="对非文档/非兼容层路径全面 fail（中期目标）"
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="加载基线文件进行比较"
    )
    parser.add_argument(
        "--save-baseline",
        type=Path,
        default=None,
        help="保存当前违规作为基线到指定文件"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认自动检测）"
    )
    args = parser.parse_args()

    # 从环境变量读取开关（命令行参数优先）
    fail_mode = args.fail or os.environ.get("ENV_DRIFT_FAIL", "0") in ("1", "true", "yes")
    fail_on_new = args.fail_on_new or os.environ.get("ENV_DRIFT_FAIL_ON_NEW", "0") in ("1", "true", "yes")
    strict_mode = args.strict or os.environ.get("ENV_DRIFT_STRICT", "0") in ("1", "true", "yes")

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

    # 确定基线文件路径
    baseline_path = args.baseline
    if baseline_path is None and (fail_on_new or strict_mode):
        # 如果启用了 fail-on-new 或 strict 但没指定基线，尝试加载默认基线
        default_baseline = project_root / DEFAULT_BASELINE_PATH
        if default_baseline.exists():
            baseline_path = default_baseline

    print(f"项目根目录: {project_root}")
    mode_desc = []
    if fail_mode:
        mode_desc.append("fail")
    if fail_on_new:
        mode_desc.append("fail-on-new")
    if strict_mode:
        mode_desc.append("strict")
    print(f"检查模式: {', '.join(mode_desc) if mode_desc else 'warning'}")
    if baseline_path:
        print(f"基线文件: {baseline_path}")

    # 加载基线
    baseline = None
    if baseline_path:
        baseline = load_baseline(baseline_path)
        if baseline:
            print(f"已加载基线，包含 {len(baseline.violations)} 个已知违规")

    # 执行扫描
    result = scan_project(project_root)

    # 保存基线（如果指定）
    if args.save_baseline:
        save_baseline(result, args.save_baseline)

    # 分类违规
    classified = classify_violations(result, project_root, baseline)

    # 打印分类报告
    print_classified_report(result, classified, fail_mode, strict_mode, fail_on_new)

    # 确定退出码
    if fail_mode and result.violations:
        return 1

    if fail_on_new and classified["new"]:
        return 1

    if strict_mode:
        # strict 模式：非文档/兼容层路径的新增违规导致失败
        strict_new = [v for v in classified["strict"] if v in classified["new"]]
        if strict_new:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
