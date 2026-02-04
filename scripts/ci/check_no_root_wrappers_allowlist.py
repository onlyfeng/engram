#!/usr/bin/env python3
"""
根目录 wrapper allowlist 校验脚本

校验 scripts/ci/no_root_wrappers_allowlist.json 的有效性：
1. JSON Schema 校验
2. expires_on 日期格式与过期状态
3. owner 非空且符合规范
4. 可选：验证每个 entry 是否命中至少一个代码位置

用法:
    python scripts/ci/check_no_root_wrappers_allowlist.py
    python scripts/ci/check_no_root_wrappers_allowlist.py --strict  # 未使用条目视为错误
    python scripts/ci/check_no_root_wrappers_allowlist.py --json    # JSON 输出
    python scripts/ci/check_no_root_wrappers_allowlist.py --skip-usage-check  # 跳过使用检查

退出码:
    0 - 校验通过
    1 - 校验失败（schema 错误、过期条目或严格模式下的未使用条目）
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ============================================================================
# 配置
# ============================================================================

# 默认文件路径（相对于项目根）
DEFAULT_ALLOWLIST_FILE = "scripts/ci/no_root_wrappers_allowlist.json"
DEFAULT_SCHEMA_FILE = "schemas/no_root_wrappers_allowlist_v2.schema.json"

# 扫描目录（用于使用检查）
SCAN_DIRECTORIES = ["src", "tests", "docs"]

# Owner 格式规范（GitHub 用户名或团队）
OWNER_PATTERN = re.compile(r"^@?[a-zA-Z0-9_-]+(-[a-zA-Z0-9_-]+)*$")

# ISO8601 日期格式
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class ValidationError:
    """单个校验错误"""

    entry_id: str
    field: str
    message: str
    severity: str = "error"  # error, warning


@dataclass
class UsageHit:
    """allowlist 条目使用情况"""

    entry_id: str
    file_path: str
    line_number: int
    line_content: str


@dataclass
class ValidationResult:
    """校验结果"""

    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)
    schema_errors: List[str] = field(default_factory=list)
    expired_entries: List[str] = field(default_factory=list)
    unused_entries: List[str] = field(default_factory=list)
    usage_hits: List[UsageHit] = field(default_factory=list)
    entries_checked: int = 0
    files_scanned: int = 0

    def has_errors(self) -> bool:
        return len(self.errors) > 0 or len(self.schema_errors) > 0

    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": not self.has_errors(),
            "entries_checked": self.entries_checked,
            "files_scanned": self.files_scanned,
            "schema_errors": self.schema_errors,
            "errors": [
                {
                    "entry_id": e.entry_id,
                    "field": e.field,
                    "message": e.message,
                    "severity": e.severity,
                }
                for e in self.errors
            ],
            "warnings": [
                {
                    "entry_id": w.entry_id,
                    "field": w.field,
                    "message": w.message,
                    "severity": w.severity,
                }
                for w in self.warnings
            ],
            "expired_entries": self.expired_entries,
            "unused_entries": self.unused_entries,
            "usage_hits": [
                {
                    "entry_id": h.entry_id,
                    "file_path": h.file_path,
                    "line_number": h.line_number,
                    "line_content": h.line_content.strip(),
                }
                for h in self.usage_hits
            ],
        }


# ============================================================================
# Schema 校验
# ============================================================================


def load_json_file(path: Path) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """加载 JSON 文件，返回 (data, error)"""
    if not path.exists():
        return None, f"文件不存在: {path}"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败: {e}"
    except OSError as e:
        return None, f"读取文件失败: {e}"


def validate_against_schema(
    data: Dict[str, Any], schema: Dict[str, Any], result: ValidationResult
) -> bool:
    """
    简单的 JSON Schema 校验（不依赖 jsonschema 库）

    校验:
    - version 字段
    - entries 数组存在
    - 每个 entry 的必需字段
    - 字段类型和格式
    """
    valid = True

    # 校验顶层结构
    if "version" not in data:
        result.schema_errors.append("缺少必需字段: version")
        valid = False

    if "entries" not in data:
        result.schema_errors.append("缺少必需字段: entries")
        valid = False
    elif not isinstance(data["entries"], list):
        result.schema_errors.append("entries 必须是数组")
        valid = False

    # 校验 version 值（支持 "2" 或 "2.0"）
    version = data.get("version")
    if version not in ("2", "2.0"):
        result.schema_errors.append(f"version 值无效，期望 '2' 或 '2.0'，实际 '{version}'")
        valid = False

    return valid


def get_entry_required_fields(schema: Dict[str, Any]) -> List[str]:
    """从 schema 获取 entry 必需字段列表"""
    entry_def = schema.get("definitions", {}).get("allowlist_entry", {})
    return entry_def.get("required", [])


def get_entry_valid_fields(schema: Dict[str, Any]) -> Set[str]:
    """从 schema 获取 entry 有效字段列表"""
    entry_def = schema.get("definitions", {}).get("allowlist_entry", {})
    return set(entry_def.get("properties", {}).keys())


# ============================================================================
# 条目校验
# ============================================================================


def validate_entry_fields(
    entry: Dict[str, Any],
    entry_id: str,
    required_fields: List[str],
    valid_fields: Set[str],
    result: ValidationResult,
) -> bool:
    """校验单个条目的字段"""
    valid = True

    # 检查必需字段（宽松模式：支持 file_pattern 作为 file_glob 的别名）
    field_aliases = {
        "file_glob": ["file_pattern", "file_glob"],
        "expires_on": ["expiry", "expires_on"],
    }

    for req_field in required_fields:
        aliases = field_aliases.get(req_field, [req_field])
        has_field = any(alias in entry and entry[alias] for alias in aliases)
        if not has_field:
            result.errors.append(
                ValidationError(
                    entry_id=entry_id,
                    field=req_field,
                    message=f"缺少必需字段: {req_field}",
                )
            )
            valid = False

    # 检查未知字段（警告）
    known_fields = valid_fields | {"file_pattern", "expiry", "ticket"}  # 兼容旧格式
    for field_name in entry.keys():
        if field_name not in known_fields:
            result.warnings.append(
                ValidationError(
                    entry_id=entry_id,
                    field=field_name,
                    message=f"未知字段: {field_name}",
                    severity="warning",
                )
            )

    return valid


def validate_date_format(date_str: str) -> bool:
    """校验日期格式是否为 YYYY-MM-DD"""
    if not DATE_PATTERN.match(date_str):
        return False
    try:
        date.fromisoformat(date_str)
        return True
    except ValueError:
        return False


def validate_expires_on(
    entry: Dict[str, Any], entry_id: str, result: ValidationResult
) -> bool:
    """校验 expires_on（或 expiry）字段"""
    # 支持两种字段名
    expires_value = entry.get("expires_on") or entry.get("expiry")

    if not expires_value:
        # expires_on 是可选字段
        return True

    # 校验格式
    if not validate_date_format(expires_value):
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="expires_on",
                message=f"日期格式无效，期望 YYYY-MM-DD，实际 '{expires_value}'",
            )
        )
        return False

    # 校验是否过期
    try:
        expiry_date = date.fromisoformat(expires_value)
        if date.today() > expiry_date:
            result.expired_entries.append(entry_id)
            result.errors.append(
                ValidationError(
                    entry_id=entry_id,
                    field="expires_on",
                    message=f"条目已过期: {expires_value}",
                )
            )
            return False
    except ValueError:
        pass  # 格式错误已在上面报告

    return True


def validate_owner(
    entry: Dict[str, Any], entry_id: str, result: ValidationResult
) -> bool:
    """校验 owner 字段"""
    owner = entry.get("owner")

    if not owner:
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="owner",
                message="owner 字段不能为空",
            )
        )
        return False

    if not isinstance(owner, str):
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="owner",
                message="owner 必须是字符串",
            )
        )
        return False

    owner = owner.strip()
    if not owner:
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="owner",
                message="owner 字段不能为空字符串",
            )
        )
        return False

    # 校验格式（GitHub 用户名或团队）
    if not OWNER_PATTERN.match(owner):
        result.warnings.append(
            ValidationError(
                entry_id=entry_id,
                field="owner",
                message=f"owner 格式不规范，建议使用 @username 或 @team-name 格式: '{owner}'",
                severity="warning",
            )
        )

    return True


def validate_id_format(
    entry: Dict[str, Any], entry_id: str, result: ValidationResult
) -> bool:
    """校验 id 字段格式"""
    if not entry_id:
        result.errors.append(
            ValidationError(
                entry_id="<unknown>",
                field="id",
                message="id 字段不能为空",
            )
        )
        return False

    # id 应该是小写字母、数字、下划线、连字符
    id_pattern = re.compile(r"^[a-z0-9_-]+$")
    if not id_pattern.match(entry_id):
        result.warnings.append(
            ValidationError(
                entry_id=entry_id,
                field="id",
                message=f"id 格式不规范，建议使用小写字母、数字、下划线和连字符: '{entry_id}'",
                severity="warning",
            )
        )

    return True


# ============================================================================
# 使用情况检查
# ============================================================================


def get_file_pattern(entry: Dict[str, Any]) -> Optional[str]:
    """获取条目的文件匹配模式"""
    return entry.get("file_glob") or entry.get("file_pattern")


def get_module(entry: Dict[str, Any]) -> Optional[str]:
    """获取条目的模块名"""
    return entry.get("module")


def check_entry_usage(
    entry: Dict[str, Any],
    entry_id: str,
    project_root: Path,
    result: ValidationResult,
) -> bool:
    """检查单个条目是否被使用"""
    file_pattern = get_file_pattern(entry)
    module = get_module(entry)

    if not file_pattern or not module:
        return False

    found = False

    # 构建导入匹配模式
    import_patterns = [
        re.compile(rf"^\s*import\s+{re.escape(module)}\b"),
        re.compile(rf"^\s*from\s+{re.escape(module)}\b\s+import\b"),
    ]

    # 扫描匹配的文件
    for scan_dir in SCAN_DIRECTORIES:
        dir_path = project_root / scan_dir
        if not dir_path.exists():
            continue

        for py_file in dir_path.rglob("*.py"):
            relative_path = str(py_file.relative_to(project_root))

            # 检查文件是否匹配 pattern
            if not fnmatch.fnmatch(relative_path, file_pattern):
                continue

            # 检查文件内容
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            for line_number, line in enumerate(content.splitlines(), 1):
                for pattern in import_patterns:
                    if pattern.match(line):
                        result.usage_hits.append(
                            UsageHit(
                                entry_id=entry_id,
                                file_path=relative_path,
                                line_number=line_number,
                                line_content=line,
                            )
                        )
                        found = True
                        break

    return found


def check_all_entries_usage(
    entries: List[Dict[str, Any]],
    project_root: Path,
    result: ValidationResult,
    strict: bool = False,
) -> None:
    """检查所有条目的使用情况"""
    files_scanned: Set[str] = set()

    # 统计扫描的文件数
    for scan_dir in SCAN_DIRECTORIES:
        dir_path = project_root / scan_dir
        if dir_path.exists():
            for py_file in dir_path.rglob("*.py"):
                files_scanned.add(str(py_file.relative_to(project_root)))

    result.files_scanned = len(files_scanned)

    # 检查每个条目
    for entry in entries:
        entry_id = entry.get("id", "<unknown>")
        used = check_entry_usage(entry, entry_id, project_root, result)

        if not used:
            result.unused_entries.append(entry_id)
            if strict:
                result.errors.append(
                    ValidationError(
                        entry_id=entry_id,
                        field="usage",
                        message="条目未被使用（未在代码中找到匹配的导入）",
                    )
                )
            else:
                result.warnings.append(
                    ValidationError(
                        entry_id=entry_id,
                        field="usage",
                        message="条目未被使用（未在代码中找到匹配的导入）",
                        severity="warning",
                    )
                )


# ============================================================================
# 主校验函数
# ============================================================================


def validate_allowlist(
    allowlist_path: Path,
    schema_path: Path,
    project_root: Path,
    check_usage: bool = True,
    strict: bool = False,
) -> ValidationResult:
    """执行完整的 allowlist 校验"""
    result = ValidationResult()

    # 加载 allowlist
    data, error = load_json_file(allowlist_path)
    if error:
        result.schema_errors.append(error)
        return result

    # 加载 schema（可选）
    schema, _ = load_json_file(schema_path)
    if schema is None:
        # 使用默认的必需字段（宽松模式）
        required_fields = ["id", "module", "owner", "reason"]
        valid_fields = {
            "id",
            "scope",
            "module",
            "file_glob",
            "file_path",
            "reason",
            "owner",
            "expires_on",
            "category",
            "created_at",
            "jira_ticket",
            "notes",
        }
    else:
        # 从 schema 获取必需字段
        required_fields = get_entry_required_fields(schema)
        valid_fields = get_entry_valid_fields(schema)

        # Schema 校验
        validate_against_schema(data, schema, result)

    # 校验每个条目
    entries = data.get("entries", [])
    result.entries_checked = len(entries)

    seen_ids: Set[str] = set()

    for entry in entries:
        entry_id = entry.get("id", "<unknown>")

        # 检查 id 唯一性
        if entry_id in seen_ids:
            result.errors.append(
                ValidationError(
                    entry_id=entry_id,
                    field="id",
                    message=f"id 重复: {entry_id}",
                )
            )
        seen_ids.add(entry_id)

        # 校验 id 格式
        validate_id_format(entry, entry_id, result)

        # 校验必需字段（使用宽松模式，支持旧字段名）
        validate_entry_fields(entry, entry_id, required_fields, valid_fields, result)

        # 校验 expires_on
        validate_expires_on(entry, entry_id, result)

        # 校验 owner
        validate_owner(entry, entry_id, result)

    # 使用情况检查
    if check_usage and entries:
        check_all_entries_usage(entries, project_root, result, strict)

    return result


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验 no_root_wrappers_allowlist.json 的有效性"
    )
    parser.add_argument(
        "--allowlist-file",
        type=Path,
        default=None,
        help=f"Allowlist 文件路径（默认: {DEFAULT_ALLOWLIST_FILE}）",
    )
    parser.add_argument(
        "--schema-file",
        type=Path,
        default=None,
        help=f"Schema 文件路径（默认: {DEFAULT_SCHEMA_FILE}）",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认自动检测）",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：未使用的条目视为错误",
    )
    parser.add_argument(
        "--skip-usage-check",
        action="store_true",
        help="跳过使用情况检查",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 格式输出",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细信息",
    )
    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = args.project_root.resolve()
    else:
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent.parent  # scripts/ci/ 的父父目录

    if not project_root.exists():
        print(f"[ERROR] 项目根目录不存在: {project_root}", file=sys.stderr)
        return 1

    # 确定文件路径
    allowlist_path = (
        args.allowlist_file.resolve()
        if args.allowlist_file
        else project_root / DEFAULT_ALLOWLIST_FILE
    )
    schema_path = (
        args.schema_file.resolve()
        if args.schema_file
        else project_root / DEFAULT_SCHEMA_FILE
    )

    # 执行校验
    result = validate_allowlist(
        allowlist_path=allowlist_path,
        schema_path=schema_path,
        project_root=project_root,
        check_usage=not args.skip_usage_check,
        strict=args.strict,
    )

    # 输出结果
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("Allowlist 校验")
        print("=" * 70)
        print()
        print(f"项目根目录: {project_root}")
        print(f"Allowlist 文件: {allowlist_path}")
        print(f"Schema 文件: {schema_path}")
        print(f"条目数: {result.entries_checked}")
        if not args.skip_usage_check:
            print(f"扫描文件数: {result.files_scanned}")
        print()

        # Schema 错误
        if result.schema_errors:
            print("[ERROR] Schema 校验失败:")
            for err in result.schema_errors:
                print(f"  - {err}")
            print()

        # 条目错误
        if result.errors:
            print(f"[ERROR] 发现 {len(result.errors)} 个错误:")
            for err in result.errors:
                print(f"  - [{err.entry_id}] {err.field}: {err.message}")
            print()

        # 警告
        if result.warnings and args.verbose:
            print(f"[WARN] 发现 {len(result.warnings)} 个警告:")
            for warn in result.warnings:
                print(f"  - [{warn.entry_id}] {warn.field}: {warn.message}")
            print()

        # 过期条目
        if result.expired_entries:
            print(f"[ERROR] 过期的条目: {result.expired_entries}")
            print()

        # 未使用条目
        if result.unused_entries:
            severity = "ERROR" if args.strict else "WARN"
            print(f"[{severity}] 未使用的条目: {result.unused_entries}")
            print()

        # 使用命中
        if result.usage_hits and args.verbose:
            print(f"[INFO] 使用情况 ({len(result.usage_hits)} 处命中):")
            for hit in result.usage_hits:
                print(f"  - [{hit.entry_id}] {hit.file_path}:{hit.line_number}")
            print()

        # 总结
        print("-" * 70)
        if result.has_errors():
            print("[FAIL] Allowlist 校验失败")
        else:
            if result.has_warnings():
                print("[OK] Allowlist 校验通过（有警告）")
            else:
                print("[OK] Allowlist 校验通过")

    return 1 if result.has_errors() else 0


if __name__ == "__main__":
    sys.exit(main())
