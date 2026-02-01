#!/usr/bin/env python3
"""
检查迭代证据文件合约（命名规范 + JSON Schema 校验）

功能:
1. 扫描 docs/acceptance/evidence/ 下的 evidence JSON 文件
2. 校验文件名符合命名规范（canonical 或 snapshot 格式）
3. 使用 schemas/iteration_evidence_v1.schema.json 校验 JSON 内容
4. （可选）检查 evidence 文件中的 iteration_number 与文件名一致性

用法:
    # 检查所有证据文件
    python scripts/ci/check_iteration_evidence_contract.py

    # 详细输出
    python scripts/ci/check_iteration_evidence_contract.py --verbose

    # 仅统计（不阻断）
    python scripts/ci/check_iteration_evidence_contract.py --stats-only

退出码:
    0 - 检查通过或 --stats-only 模式
    1 - 检查失败（存在违规）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

# 尝试导入 jsonschema，如果不可用则标记
try:
    import jsonschema

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

# ============================================================================
# 项目路径配置
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录。"""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


# 证据目录
EVIDENCE_DIR = get_project_root() / "docs" / "acceptance" / "evidence"

# Schema 文件路径
SCHEMA_PATH = get_project_root() / "schemas" / "iteration_evidence_v1.schema.json"


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class EvidenceViolation:
    """证据文件违规记录。"""

    file: Path
    violation_type: str  # "naming", "schema", "content", "missing"
    message: str

    def __str__(self) -> str:
        rel_path = self.file.name
        return f"{rel_path}: [{self.violation_type}] {self.message}"


# ============================================================================
# 文件名模式（基于 iteration_evidence_naming.py 规范）
# ============================================================================

# Canonical 格式: iteration_{N}_evidence.json
CANONICAL_PATTERN = re.compile(r"^iteration_(\d+)_evidence\.json$")

# Snapshot 格式（无 SHA）: iteration_{N}_{timestamp}.json
SNAPSHOT_PATTERN = re.compile(r"^iteration_(\d+)_(\d{8}_\d{6})\.json$")

# Snapshot 格式（带 SHA）: iteration_{N}_{timestamp}_{sha7}.json
SNAPSHOT_SHA_PATTERN = re.compile(r"^iteration_(\d+)_(\d{8}_\d{6})_([a-f0-9]{7})\.json$")


def parse_evidence_filename(filename: str) -> Optional[dict[str, Any]]:
    """解析证据文件名，提取迭代编号、时间戳、SHA 等信息。

    支持的格式:
        - iteration_{N}_evidence.json (canonical)
        - iteration_{N}_{timestamp}.json (snapshot)
        - iteration_{N}_{timestamp}_{sha7}.json (snapshot with SHA)

    Args:
        filename: 文件名字符串

    Returns:
        包含解析结果的字典，或 None（如果格式无效）:
        - iteration_number: int
        - is_canonical: bool
        - timestamp: Optional[str]
        - commit_sha: Optional[str]
    """
    # Canonical 格式
    match = CANONICAL_PATTERN.match(filename)
    if match:
        return {
            "iteration_number": int(match.group(1)),
            "is_canonical": True,
            "timestamp": None,
            "commit_sha": None,
        }

    # Snapshot 格式（带 SHA）
    match = SNAPSHOT_SHA_PATTERN.match(filename)
    if match:
        return {
            "iteration_number": int(match.group(1)),
            "is_canonical": False,
            "timestamp": match.group(2),
            "commit_sha": match.group(3),
        }

    # Snapshot 格式（无 SHA）
    match = SNAPSHOT_PATTERN.match(filename)
    if match:
        return {
            "iteration_number": int(match.group(1)),
            "is_canonical": False,
            "timestamp": match.group(2),
            "commit_sha": None,
        }

    return None


# ============================================================================
# 扫描与校验逻辑
# ============================================================================


def get_evidence_files(evidence_dir: Path) -> List[Path]:
    """获取证据目录下的所有 JSON 文件。

    排除:
    - .gitkeep
    - 非 .json 文件

    Args:
        evidence_dir: 证据目录路径

    Returns:
        JSON 文件路径列表（已排序）
    """
    if not evidence_dir.exists():
        return []

    files = []
    for filepath in evidence_dir.iterdir():
        if filepath.is_file() and filepath.suffix == ".json":
            files.append(filepath)

    return sorted(files)


def load_schema() -> Optional[dict[str, Any]]:
    """加载 JSON Schema。

    Returns:
        Schema 字典，或 None（如果加载失败）
    """
    if not SCHEMA_PATH.exists():
        return None

    try:
        with SCHEMA_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def validate_filename(filepath: Path) -> Optional[EvidenceViolation]:
    """校验证据文件名是否符合命名规范。

    Args:
        filepath: 文件路径

    Returns:
        EvidenceViolation 如果违规，否则 None
    """
    parsed = parse_evidence_filename(filepath.name)
    if parsed is None:
        return EvidenceViolation(
            file=filepath,
            violation_type="naming",
            message=(
                f"文件名 '{filepath.name}' 不符合命名规范。"
                f"有效格式: iteration_{{N}}_evidence.json (canonical) "
                f"或 iteration_{{N}}_{{timestamp}}.json (snapshot)"
            ),
        )
    return None


def validate_json_content(
    filepath: Path, schema: Optional[dict[str, Any]]
) -> List[EvidenceViolation]:
    """校验证据文件的 JSON 内容。

    包括:
    1. JSON 语法校验
    2. JSON Schema 校验（如果 schema 可用）
    3. iteration_number 与文件名一致性校验

    Args:
        filepath: 文件路径
        schema: JSON Schema 字典（可选）

    Returns:
        违规列表
    """
    violations: List[EvidenceViolation] = []

    # 1. 加载并解析 JSON
    try:
        with filepath.open(encoding="utf-8") as f:
            content = json.load(f)
    except json.JSONDecodeError as e:
        violations.append(
            EvidenceViolation(
                file=filepath,
                violation_type="content",
                message=f"JSON 解析失败: {e}",
            )
        )
        return violations
    except OSError as e:
        violations.append(
            EvidenceViolation(
                file=filepath,
                violation_type="content",
                message=f"无法读取文件: {e}",
            )
        )
        return violations

    # 2. JSON Schema 校验
    if schema is not None and HAS_JSONSCHEMA:
        try:
            jsonschema.validate(content, schema)
        except jsonschema.ValidationError as e:
            # 提取简短错误信息
            error_path = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "(root)"
            violations.append(
                EvidenceViolation(
                    file=filepath,
                    violation_type="schema",
                    message=f"Schema 校验失败 @ {error_path}: {e.message}",
                )
            )
        except jsonschema.SchemaError as e:
            violations.append(
                EvidenceViolation(
                    file=filepath,
                    violation_type="schema",
                    message=f"Schema 本身无效: {e.message}",
                )
            )

    # 3. iteration_number 与文件名一致性校验
    # 仅当 content 是字典时执行此校验
    if isinstance(content, dict):
        parsed = parse_evidence_filename(filepath.name)
        if parsed is not None:
            file_iter_num = parsed["iteration_number"]
            content_iter_num = content.get("iteration_number")

            if content_iter_num is not None and content_iter_num != file_iter_num:
                violations.append(
                    EvidenceViolation(
                        file=filepath,
                        violation_type="content",
                        message=(
                            f"iteration_number 不一致: 文件名指示 {file_iter_num}，"
                            f"JSON 内容为 {content_iter_num}"
                        ),
                    )
                )

    return violations


def scan_evidence_files(
    evidence_dir: Optional[Path] = None,
    verbose: bool = False,
) -> tuple[List[EvidenceViolation], int]:
    """扫描并校验所有证据文件。

    Args:
        evidence_dir: 证据目录（默认使用 EVIDENCE_DIR）
        verbose: 是否显示详细输出

    Returns:
        (违规列表, 总扫描文件数)
    """
    if evidence_dir is None:
        evidence_dir = EVIDENCE_DIR

    violations: List[EvidenceViolation] = []

    # 获取文件列表
    files = get_evidence_files(evidence_dir)

    if not files:
        if verbose:
            print(f"[INFO] 证据目录为空或不存在: {evidence_dir}")
        return [], 0

    if verbose:
        print(f"[INFO] 将检查 {len(files)} 个证据文件")
        for f in files:
            print(f"       - {f.name}")
        print()

    # 加载 Schema
    schema = load_schema()
    if schema is None:
        if verbose:
            print("[WARN] 无法加载 JSON Schema，将跳过 Schema 校验")
    elif not HAS_JSONSCHEMA:
        if verbose:
            print("[WARN] jsonschema 库未安装，将跳过 Schema 校验")

    # 扫描每个文件
    for filepath in files:
        # 1. 文件名校验
        naming_violation = validate_filename(filepath)
        if naming_violation:
            violations.append(naming_violation)

        # 2. JSON 内容校验
        content_violations = validate_json_content(filepath, schema)
        violations.extend(content_violations)

        if verbose and (naming_violation or content_violations):
            print(f"  ❌ {filepath.name}: {1 + len(content_violations)} 个违规")

    return violations, len(files)


# ============================================================================
# 报告输出
# ============================================================================


def print_report(
    violations: List[EvidenceViolation],
    total_files: int,
    verbose: bool = False,
) -> None:
    """打印检查报告。

    Args:
        violations: 违规列表
        total_files: 总扫描文件数
        verbose: 是否显示详细输出
    """
    print()
    print("=" * 70)
    print("迭代证据文件合约检查报告")
    print("=" * 70)
    print()

    print(f"扫描文件数:      {total_files}")
    print(f"违规条目数:      {len(violations)}")

    # 按类型统计
    naming_count = sum(1 for v in violations if v.violation_type == "naming")
    schema_count = sum(1 for v in violations if v.violation_type == "schema")
    content_count = sum(1 for v in violations if v.violation_type == "content")
    print(f"  - 命名不合规:  {naming_count}")
    print(f"  - Schema 不合规: {schema_count}")
    print(f"  - 内容不合规:  {content_count}")
    print()

    if violations:
        print("违规列表:")
        print("-" * 70)

        # 按文件分组
        by_file: dict[Path, List[EvidenceViolation]] = {}
        for v in violations:
            by_file.setdefault(v.file, []).append(v)

        for file_path, vlist in sorted(by_file.items()):
            print(f"\n【{file_path.name}】({len(vlist)} 条)")

            for v in vlist:
                print(f"  [{v.violation_type}] {v.message}")

        print()
        print("-" * 70)
        print()
        print("修复指南:")
        print()
        print("  1. 命名不合规:")
        print("     将文件重命名为符合规范的格式:")
        print("     - Canonical: iteration_{N}_evidence.json")
        print("     - Snapshot: iteration_{N}_{YYYYMMDD_HHMMSS}.json")
        print("     - Snapshot+SHA: iteration_{N}_{YYYYMMDD_HHMMSS}_{sha7}.json")
        print()
        print("  2. Schema 不合规:")
        print("     根据 schemas/iteration_evidence_v1.schema.json 修复 JSON 内容")
        print("     必需字段: iteration_number, recorded_at, commit_sha, runner, commands")
        print()
        print("  3. 内容不合规:")
        print("     确保 JSON 内容与文件名中的迭代编号一致")
        print()
        print("  参考:")
        print("     - 命名规范: scripts/iteration/iteration_evidence_naming.py")
        print("     - Schema 定义: schemas/iteration_evidence_v1.schema.json")
        print("     - 模板: docs/acceptance/_templates/iteration_evidence.template.json")
        print()
    else:
        print("[OK] 所有证据文件符合合约规范")


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查迭代证据文件合约（命名规范 + JSON Schema 校验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细输出",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="仅统计，不阻断（始终返回 0）",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="证据目录路径（默认: docs/acceptance/evidence/）",
    )

    args = parser.parse_args()

    evidence_dir = args.evidence_dir if args.evidence_dir else EVIDENCE_DIR

    print("=" * 70)
    print("迭代证据文件合约检查")
    print("=" * 70)
    print()

    violations, total_files = scan_evidence_files(
        evidence_dir=evidence_dir,
        verbose=args.verbose,
    )

    print_report(
        violations,
        total_files,
        verbose=args.verbose,
    )

    # 确定退出码
    if args.stats_only:
        print()
        print("[INFO] --stats-only 模式: 仅统计，不阻断")
        print("[OK] 退出码: 0")
        return 0

    if violations:
        print()
        print(f"[FAIL] 存在 {len(violations)} 个违规")
        print("[FAIL] 退出码: 1")
        return 1

    print()
    print("[OK] 所有检查通过")
    print("[OK] 退出码: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
