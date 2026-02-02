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
    violation_type: str  # "naming", "schema", "content", "missing", "link"
    message: str

    def __str__(self) -> str:
        rel_path = self.file.name
        return f"{rel_path}: [{self.violation_type}] {self.message}"


@dataclass
class EvidenceWarning:
    """证据文件警告记录（不阻断 CI）。"""

    file: Path
    warning_type: str  # "missing_links", "suggestion"
    message: str

    def __str__(self) -> str:
        rel_path = self.file.name
        return f"{rel_path}: [{self.warning_type}] {self.message}"


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
# Regression Doc 相关配置
# ============================================================================

# Regression 文档目录
ACCEPTANCE_DIR = get_project_root() / "docs" / "acceptance"

# Regression 文档命名格式: iteration_{N}_regression.md
REGRESSION_DOC_PATTERN = re.compile(r"^iteration_(\d+)_regression\.md$")

# Evidence 文件引用模式（在 regression 文档中）
# 匹配: [xxx](evidence/iteration_N_evidence.json) 或 evidence/iteration_N_evidence.json
EVIDENCE_REFERENCE_PATTERN = re.compile(
    r"(?:\[.*?\]\()?evidence/iteration_(\d+)_evidence\.json\)?"
)


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


def validate_regression_doc_link(
    filepath: Path,
    content: dict[str, Any],
    project_root: Optional[Path] = None,
) -> tuple[List[EvidenceViolation], List[EvidenceWarning]]:
    """校验 evidence 文件中的 links.regression_doc_url。

    校验规则:
    1. 若 links.regression_doc_url 存在：
       - 校验指向的文件存在
       - 校验文件名符合 iteration_{N}_regression.md 格式
       - 校验 N 与 evidence 的 iteration_number 一致
    2. 若 links 或 regression_doc_url 不存在：
       - 对 canonical 文件输出 warn（历史 evidence 可能缺少 links）

    Args:
        filepath: evidence 文件路径
        content: evidence JSON 内容
        project_root: 项目根目录（默认自动获取）

    Returns:
        (违规列表, 警告列表)
    """
    violations: List[EvidenceViolation] = []
    warnings: List[EvidenceWarning] = []

    if project_root is None:
        project_root = get_project_root()

    # 解析文件名获取迭代编号
    parsed = parse_evidence_filename(filepath.name)
    if parsed is None:
        # 文件名无效，跳过此校验（会由 validate_filename 报告）
        return violations, warnings

    file_iter_num = parsed["iteration_number"]
    is_canonical = parsed["is_canonical"]

    # 检查是否存在 links
    links = content.get("links")
    if links is None:
        # 只对 canonical 文件发出警告
        if is_canonical:
            warnings.append(
                EvidenceWarning(
                    file=filepath,
                    warning_type="missing_links",
                    message=(
                        f"缺少 links 字段。建议使用 record script 补写 links，"
                        f"或手动添加 links.regression_doc_url 指向 "
                        f"docs/acceptance/iteration_{file_iter_num}_regression.md"
                    ),
                )
            )
        return violations, warnings

    # 检查是否存在 regression_doc_url
    regression_doc_url = links.get("regression_doc_url")
    if regression_doc_url is None:
        # 只对 canonical 文件发出警告
        if is_canonical:
            warnings.append(
                EvidenceWarning(
                    file=filepath,
                    warning_type="missing_links",
                    message=(
                        f"links 中缺少 regression_doc_url。建议添加: "
                        f'"regression_doc_url": "docs/acceptance/iteration_{file_iter_num}_regression.md"'
                    ),
                )
            )
        return violations, warnings

    # 校验 regression_doc_url 指向的文件
    # 支持相对路径（相对于项目根目录）
    if regression_doc_url.startswith("http://") or regression_doc_url.startswith("https://"):
        # URL 形式，暂不校验
        return violations, warnings

    # 相对路径
    regression_doc_path = project_root / regression_doc_url

    # 1. 校验文件存在
    if not regression_doc_path.exists():
        violations.append(
            EvidenceViolation(
                file=filepath,
                violation_type="link",
                message=(
                    f"links.regression_doc_url 指向的文件不存在: {regression_doc_url}"
                ),
            )
        )
        return violations, warnings

    # 2. 校验文件名格式
    doc_filename = regression_doc_path.name
    doc_match = REGRESSION_DOC_PATTERN.match(doc_filename)
    if not doc_match:
        violations.append(
            EvidenceViolation(
                file=filepath,
                violation_type="link",
                message=(
                    f"links.regression_doc_url 指向的文件名不符合规范: {doc_filename}。"
                    f"期望格式: iteration_{{N}}_regression.md"
                ),
            )
        )
        return violations, warnings

    # 3. 校验 iteration_number 一致性
    doc_iter_num = int(doc_match.group(1))
    content_iter_num = content.get("iteration_number")

    if content_iter_num is not None and doc_iter_num != content_iter_num:
        violations.append(
            EvidenceViolation(
                file=filepath,
                violation_type="link",
                message=(
                    f"regression_doc_url 的迭代编号不一致: "
                    f"evidence 文件 iteration_number={content_iter_num}，"
                    f"但 regression_doc_url 指向 iteration_{doc_iter_num}_regression.md"
                ),
            )
        )

    return violations, warnings


def validate_bidirectional_reference(
    filepath: Path,
    content: dict[str, Any],
    project_root: Optional[Path] = None,
) -> List[EvidenceViolation]:
    """校验 regression 文档是否引用了对应的 canonical evidence 文件（双向一致性）。

    只对 canonical evidence 文件进行此校验。

    Args:
        filepath: canonical evidence 文件路径
        content: evidence JSON 内容
        project_root: 项目根目录（默认自动获取）

    Returns:
        违规列表
    """
    violations: List[EvidenceViolation] = []

    if project_root is None:
        project_root = get_project_root()

    # 只对 canonical 文件进行双向校验
    parsed = parse_evidence_filename(filepath.name)
    if parsed is None or not parsed["is_canonical"]:
        return violations

    file_iter_num = parsed["iteration_number"]

    # 获取对应的 regression 文档路径
    regression_doc_path = project_root / "docs" / "acceptance" / f"iteration_{file_iter_num}_regression.md"

    if not regression_doc_path.exists():
        # regression 文档不存在，不进行双向校验（可能是新迭代）
        return violations

    # 读取 regression 文档内容
    try:
        doc_content = regression_doc_path.read_text(encoding="utf-8")
    except OSError:
        # 无法读取，跳过
        return violations

    # 查找对 canonical evidence 文件的引用
    expected_evidence_ref = f"iteration_{file_iter_num}_evidence.json"

    # 检查文档中是否引用了该 evidence 文件
    if expected_evidence_ref not in doc_content:
        violations.append(
            EvidenceViolation(
                file=filepath,
                violation_type="link",
                message=(
                    f"双向引用不一致: regression 文档 "
                    f"(iteration_{file_iter_num}_regression.md) "
                    f"未引用 canonical evidence 文件 ({expected_evidence_ref})。"
                    f"建议在 regression 文档的「验收证据」章节添加对该 evidence 文件的引用。"
                ),
            )
        )

    return violations


def scan_evidence_files(
    evidence_dir: Optional[Path] = None,
    verbose: bool = False,
    project_root: Optional[Path] = None,
) -> tuple[List[EvidenceViolation], List[EvidenceWarning], int]:
    """扫描并校验所有证据文件。

    Args:
        evidence_dir: 证据目录（默认使用 EVIDENCE_DIR）
        verbose: 是否显示详细输出
        project_root: 项目根目录（默认自动获取）

    Returns:
        (违规列表, 警告列表, 总扫描文件数)
    """
    if evidence_dir is None:
        evidence_dir = EVIDENCE_DIR

    if project_root is None:
        project_root = get_project_root()

    violations: List[EvidenceViolation] = []
    warnings: List[EvidenceWarning] = []

    # 获取文件列表
    files = get_evidence_files(evidence_dir)

    if not files:
        if verbose:
            print(f"[INFO] 证据目录为空或不存在: {evidence_dir}")
        return [], [], 0

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
        file_violations: List[EvidenceViolation] = []
        file_warnings: List[EvidenceWarning] = []

        # 1. 文件名校验
        naming_violation = validate_filename(filepath)
        if naming_violation:
            file_violations.append(naming_violation)

        # 2. JSON 内容校验
        content_violations = validate_json_content(filepath, schema)
        file_violations.extend(content_violations)

        # 3. regression_doc_url 校验和双向一致性校验
        # 需要先加载 JSON 内容
        content = None
        try:
            with filepath.open(encoding="utf-8") as f:
                content = json.load(f)
        except (json.JSONDecodeError, OSError):
            # 已由 validate_json_content 报告错误
            pass

        if content is not None and isinstance(content, dict):
            # 3a. regression_doc_url 校验
            link_violations, link_warnings = validate_regression_doc_link(
                filepath, content, project_root
            )
            file_violations.extend(link_violations)
            file_warnings.extend(link_warnings)

            # 3b. 双向一致性校验
            bidirectional_violations = validate_bidirectional_reference(
                filepath, content, project_root
            )
            file_violations.extend(bidirectional_violations)

        violations.extend(file_violations)
        warnings.extend(file_warnings)

        if verbose:
            if file_violations:
                print(f"  ❌ {filepath.name}: {len(file_violations)} 个违规")
            if file_warnings:
                print(f"  ⚠️  {filepath.name}: {len(file_warnings)} 个警告")

    return violations, warnings, len(files)


# ============================================================================
# 报告输出
# ============================================================================


def print_report(
    violations: List[EvidenceViolation],
    warnings: List[EvidenceWarning],
    total_files: int,
    verbose: bool = False,
) -> None:
    """打印检查报告。

    Args:
        violations: 违规列表
        warnings: 警告列表
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
    print(f"警告条目数:      {len(warnings)}")

    # 按类型统计违规
    naming_count = sum(1 for v in violations if v.violation_type == "naming")
    schema_count = sum(1 for v in violations if v.violation_type == "schema")
    content_count = sum(1 for v in violations if v.violation_type == "content")
    link_count = sum(1 for v in violations if v.violation_type == "link")
    print(f"  - 命名不合规:  {naming_count}")
    print(f"  - Schema 不合规: {schema_count}")
    print(f"  - 内容不合规:  {content_count}")
    print(f"  - 链接不合规:  {link_count}")
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
        print("  4. 链接不合规:")
        print("     - 确保 links.regression_doc_url 指向存在的文件")
        print("     - 文件名应为 iteration_{N}_regression.md 格式")
        print("     - N 应与 evidence 的 iteration_number 一致")
        print("     - regression 文档应引用对应的 canonical evidence 文件")
        print()
        print("  参考:")
        print("     - 命名规范: scripts/iteration/iteration_evidence_naming.py")
        print("     - Schema 定义: schemas/iteration_evidence_v1.schema.json")
        print("     - 模板: docs/acceptance/_templates/iteration_evidence.template.json")
        print()
    else:
        print("[OK] 所有证据文件符合合约规范")

    # 打印警告（不阻断）
    if warnings:
        print()
        print("=" * 70)
        print("警告（不阻断 CI）")
        print("=" * 70)
        print()

        # 按文件分组
        by_file_warn: dict[Path, List[EvidenceWarning]] = {}
        for w in warnings:
            by_file_warn.setdefault(w.file, []).append(w)

        for file_path, wlist in sorted(by_file_warn.items()):
            print(f"【{file_path.name}】({len(wlist)} 条警告)")
            for w in wlist:
                print(f"  [{w.warning_type}] {w.message}")
            print()

        print("修复建议:")
        print()
        print("  对于历史 evidence 文件缺少 links 的情况:")
        print("  1. 使用 record script 重新生成（推荐）:")
        print("     python scripts/iteration/record_iteration_evidence.py --iteration N")
        print()
        print("  2. 或手动添加 links 字段:")
        print('     "links": {')
        print('       "regression_doc_url": "docs/acceptance/iteration_N_regression.md"')
        print("     }")
        print()


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查迭代证据文件合约（命名规范 + JSON Schema 校验 + 链接一致性）",
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

    violations, warnings, total_files = scan_evidence_files(
        evidence_dir=evidence_dir,
        verbose=args.verbose,
    )

    print_report(
        violations,
        warnings,
        total_files,
        verbose=args.verbose,
    )

    # 确定退出码
    if args.stats_only:
        print()
        print("[INFO] --stats-only 模式: 仅统计，不阻断")
        if warnings:
            print(f"[WARN] 存在 {len(warnings)} 个警告（不阻断）")
        print("[OK] 退出码: 0")
        return 0

    if violations:
        print()
        print(f"[FAIL] 存在 {len(violations)} 个违规")
        if warnings:
            print(f"[WARN] 另有 {len(warnings)} 个警告（不阻断）")
        print("[FAIL] 退出码: 1")
        return 1

    print()
    if warnings:
        print(f"[WARN] 存在 {len(warnings)} 个警告（不阻断）")
    print("[OK] 所有检查通过")
    print("[OK] 退出码: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
