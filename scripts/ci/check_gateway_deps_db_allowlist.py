#!/usr/bin/env python3
"""
Gateway deps.db allowlist 校验脚本

校验 scripts/ci/gateway_deps_db_allowlist.json 的有效性：
1. JSON Schema 校验
2. 重复 id 校验
3. expires_on 日期格式与过期状态
4. owner 格式校验

用法:
    python scripts/ci/check_gateway_deps_db_allowlist.py
    python scripts/ci/check_gateway_deps_db_allowlist.py --json    # JSON 输出
    python scripts/ci/check_gateway_deps_db_allowlist.py --verbose # 详细输出

退出码:
    0 - 校验通过
    1 - 校验失败（schema 错误、重复 id、过期条目或 owner 格式错误）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from scripts.ci._date_utils import utc_today

# ============================================================================
# 配置
# ============================================================================

# 默认文件路径（相对于项目根）
DEFAULT_ALLOWLIST_FILE = "scripts/ci/gateway_deps_db_allowlist.json"
DEFAULT_SCHEMA_FILE = "schemas/gateway_deps_db_allowlist_v2.schema.json"

# Owner 格式规范（GitHub 用户名或团队，必须以 @ 开头）
OWNER_PATTERN = re.compile(r"^@[a-zA-Z0-9_-]+(-[a-zA-Z0-9_-]+)*$")

# ISO8601 日期格式
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ID 格式规范（小写字母、数字、下划线、连字符）
ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")

# Category 枚举值
VALID_CATEGORIES = {"adapter_internal", "migration_script", "legacy_compat", "testing", "other"}

# 即将过期预警阈值（天）
DEFAULT_EXPIRING_SOON_DAYS = 14

# 最大过期期限（天）- 超过此期限建议需要审批
DEFAULT_MAX_EXPIRY_DAYS = 180  # 6 个月


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
class ExpiringSoonEntry:
    """即将过期的条目"""

    entry_id: str
    expires_on: str
    days_until_expiry: int
    owner: str
    category: str = "other"


@dataclass
class ExceedsMaxExpiryEntry:
    """超过最大期限的条目"""

    entry_id: str
    expires_on: str
    days_until_expiry: int
    owner: str
    category: str = "other"
    max_expiry_days: int = DEFAULT_MAX_EXPIRY_DAYS


@dataclass
class ValidationResult:
    """校验结果"""

    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)
    schema_errors: List[str] = field(default_factory=list)
    expired_entries: List[str] = field(default_factory=list)
    expiring_soon_entries: List[ExpiringSoonEntry] = field(default_factory=list)
    exceeds_max_expiry_entries: List[ExceedsMaxExpiryEntry] = field(default_factory=list)
    duplicate_ids: List[str] = field(default_factory=list)
    entries_checked: int = 0
    # 按 category/owner 汇总统计
    category_summary: Dict[str, int] = field(default_factory=dict)
    owner_summary: Dict[str, int] = field(default_factory=dict)

    def has_errors(self) -> bool:
        return len(self.errors) > 0 or len(self.schema_errors) > 0

    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": not self.has_errors(),
            "entries_checked": self.entries_checked,
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
            "expiring_soon_entries": [
                {
                    "entry_id": e.entry_id,
                    "expires_on": e.expires_on,
                    "days_until_expiry": e.days_until_expiry,
                    "owner": e.owner,
                    "category": e.category,
                }
                for e in self.expiring_soon_entries
            ],
            "exceeds_max_expiry_entries": [
                {
                    "entry_id": e.entry_id,
                    "expires_on": e.expires_on,
                    "days_until_expiry": e.days_until_expiry,
                    "owner": e.owner,
                    "category": e.category,
                    "max_expiry_days": e.max_expiry_days,
                }
                for e in self.exceeds_max_expiry_entries
            ],
            "duplicate_ids": self.duplicate_ids,
            "category_summary": self.category_summary,
            "owner_summary": self.owner_summary,
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

    for req_field in required_fields:
        if req_field not in entry or not entry[req_field]:
            result.errors.append(
                ValidationError(
                    entry_id=entry_id,
                    field=req_field,
                    message=f"缺少必需字段: {req_field}",
                )
            )
            valid = False

    # 检查未知字段（警告）
    for field_name in entry.keys():
        if field_name not in valid_fields:
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
    entry: Dict[str, Any],
    entry_id: str,
    result: ValidationResult,
    today: date | None = None,
    expiring_soon_days: int = DEFAULT_EXPIRING_SOON_DAYS,
    max_expiry_days: int = DEFAULT_MAX_EXPIRY_DAYS,
    fail_on_max_expiry: bool = False,
) -> bool:
    """校验 expires_on 字段

    Args:
        entry: 条目数据
        entry_id: 条目 ID
        result: 校验结果对象
        today: 当前日期（用于测试注入）
        expiring_soon_days: 即将过期预警天数（默认 14 天）
        max_expiry_days: 最大过期期限（默认 180 天/6 个月）
        fail_on_max_expiry: 超过最大期限时是否失败（默认 False，仅警告）

    Returns:
        True 如果校验通过，False 如果有错误
    """
    expires_value = entry.get("expires_on")
    owner = entry.get("owner", "unknown")
    category = entry.get("category", "other")

    if not expires_value:
        # expires_on 是必需字段
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="expires_on",
                message="缺少必需字段: expires_on",
            )
        )
        return False

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
    check_date = today if today is not None else utc_today()
    try:
        expiry_date = date.fromisoformat(expires_value)
        days_until_expiry = (expiry_date - check_date).days

        # 检查是否已过期
        if check_date > expiry_date:
            result.expired_entries.append(entry_id)
            result.errors.append(
                ValidationError(
                    entry_id=entry_id,
                    field="expires_on",
                    message=f"条目已过期: {expires_value}",
                )
            )
            return False

        # 检查是否即将过期（14 天内）
        if days_until_expiry <= expiring_soon_days:
            result.expiring_soon_entries.append(
                ExpiringSoonEntry(
                    entry_id=entry_id,
                    expires_on=expires_value,
                    days_until_expiry=days_until_expiry,
                    owner=owner,
                    category=category,
                )
            )
            result.warnings.append(
                ValidationError(
                    entry_id=entry_id,
                    field="expires_on",
                    message=f"条目即将过期（{days_until_expiry} 天后）: {expires_value}",
                    severity="warning",
                )
            )

        # 检查是否超过最大期限（6 个月）
        if days_until_expiry > max_expiry_days:
            result.exceeds_max_expiry_entries.append(
                ExceedsMaxExpiryEntry(
                    entry_id=entry_id,
                    expires_on=expires_value,
                    days_until_expiry=days_until_expiry,
                    owner=owner,
                    category=category,
                    max_expiry_days=max_expiry_days,
                )
            )
            if fail_on_max_expiry:
                result.errors.append(
                    ValidationError(
                        entry_id=entry_id,
                        field="expires_on",
                        message=(
                            f"条目过期日期超过最大期限（{max_expiry_days} 天），"
                            f"当前距离过期 {days_until_expiry} 天，需要审批"
                        ),
                    )
                )
            else:
                result.warnings.append(
                    ValidationError(
                        entry_id=entry_id,
                        field="expires_on",
                        message=(
                            f"条目过期日期超过建议期限（{max_expiry_days} 天），"
                            f"当前距离过期 {days_until_expiry} 天，建议审批"
                        ),
                        severity="warning",
                    )
                )

    except ValueError:
        pass  # 格式错误已在上面报告

    return True


def validate_owner(entry: Dict[str, Any], entry_id: str, result: ValidationResult) -> bool:
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

    # 校验格式（GitHub 用户名或团队，必须以 @ 开头）
    if not OWNER_PATTERN.match(owner):
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="owner",
                message=f"owner 格式无效，必须以 @ 开头且符合 @username 或 @team-name 格式: '{owner}'",
            )
        )
        return False

    return True


def validate_id_format(entry: Dict[str, Any], entry_id: str, result: ValidationResult) -> bool:
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
    if not ID_PATTERN.match(entry_id):
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="id",
                message=f"id 格式无效，必须是小写字母、数字、下划线和连字符: '{entry_id}'",
            )
        )
        return False

    return True


def validate_category(entry: Dict[str, Any], entry_id: str, result: ValidationResult) -> bool:
    """校验 category 字段"""
    category = entry.get("category")

    if not category:
        # category 是必需字段
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="category",
                message="缺少必需字段: category",
            )
        )
        return False

    if category not in VALID_CATEGORIES:
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="category",
                message=f"category 值无效，期望 {sorted(VALID_CATEGORIES)}，实际 '{category}'",
            )
        )
        return False

    return True


def validate_reason_length(
    entry: Dict[str, Any], entry_id: str, result: ValidationResult, min_length: int = 10
) -> bool:
    """校验 reason 字段长度"""
    reason = entry.get("reason", "")

    if not reason:
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="reason",
                message="缺少必需字段: reason",
            )
        )
        return False

    if len(reason) < min_length:
        result.errors.append(
            ValidationError(
                entry_id=entry_id,
                field="reason",
                message=f"reason 长度不足，期望至少 {min_length} 字符，实际 {len(reason)} 字符",
            )
        )
        return False

    return True


# ============================================================================
# 主校验函数
# ============================================================================


def validate_allowlist(
    allowlist_path: Path,
    schema_path: Path,
    today: date | None = None,
    expiring_soon_days: int = DEFAULT_EXPIRING_SOON_DAYS,
    max_expiry_days: int = DEFAULT_MAX_EXPIRY_DAYS,
    fail_on_max_expiry: bool = False,
) -> ValidationResult:
    """执行完整的 allowlist 校验

    Args:
        allowlist_path: allowlist 文件路径
        schema_path: schema 文件路径
        today: 当前日期（用于测试注入）
        expiring_soon_days: 即将过期预警天数（默认 14 天）
        max_expiry_days: 最大过期期限（默认 180 天/6 个月）
        fail_on_max_expiry: 超过最大期限时是否失败（默认 False，仅警告）

    Returns:
        ValidationResult 校验结果
    """
    result = ValidationResult()

    # 加载 allowlist
    data, error = load_json_file(allowlist_path)
    if error:
        result.schema_errors.append(error)
        return result

    # 加载 schema（可选）
    schema, _ = load_json_file(schema_path)
    if schema is None:
        # 使用默认的必需字段
        required_fields = ["id", "file_glob", "reason", "owner", "category", "expires_on"]
        valid_fields = {
            "id",
            "file_glob",
            "file_path",
            "reason",
            "owner",
            "expires_on",
            "category",
            "created_at",
            "ticket",
            "notes",
            "migration_target",
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
        category = entry.get("category", "other")
        owner = entry.get("owner", "unknown")

        # 收集 category/owner 汇总统计
        result.category_summary[category] = result.category_summary.get(category, 0) + 1
        result.owner_summary[owner] = result.owner_summary.get(owner, 0) + 1

        # 检查 id 唯一性
        if entry_id in seen_ids:
            result.duplicate_ids.append(entry_id)
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

        # 校验必需字段
        validate_entry_fields(entry, entry_id, required_fields, valid_fields, result)

        # 校验 expires_on（包含即将过期和超过最大期限检测）
        validate_expires_on(
            entry,
            entry_id,
            result,
            today,
            expiring_soon_days=expiring_soon_days,
            max_expiry_days=max_expiry_days,
            fail_on_max_expiry=fail_on_max_expiry,
        )

        # 校验 owner
        validate_owner(entry, entry_id, result)

        # 校验 category
        validate_category(entry, entry_id, result)

        # 校验 reason 长度
        validate_reason_length(entry, entry_id, result)

    return result


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 gateway_deps_db_allowlist.json 的有效性")
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
    # 即将过期预警相关参数
    parser.add_argument(
        "--expiring-soon-days",
        type=int,
        default=DEFAULT_EXPIRING_SOON_DAYS,
        help=f"即将过期预警天数（默认: {DEFAULT_EXPIRING_SOON_DAYS}）",
    )
    parser.add_argument(
        "--max-expiry-days",
        type=int,
        default=DEFAULT_MAX_EXPIRY_DAYS,
        help=f"最大过期期限天数（默认: {DEFAULT_MAX_EXPIRY_DAYS}，约 6 个月）",
    )
    parser.add_argument(
        "--fail-on-max-expiry",
        action="store_true",
        help="超过最大期限时失败（默认仅警告）",
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
        args.schema_file.resolve() if args.schema_file else project_root / DEFAULT_SCHEMA_FILE
    )

    # 执行校验
    result = validate_allowlist(
        allowlist_path=allowlist_path,
        schema_path=schema_path,
        expiring_soon_days=args.expiring_soon_days,
        max_expiry_days=args.max_expiry_days,
        fail_on_max_expiry=args.fail_on_max_expiry,
    )

    # 输出结果
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("Gateway deps.db Allowlist 校验")
        print("=" * 70)
        print()
        print(f"项目根目录: {project_root}")
        print(f"Allowlist 文件: {allowlist_path}")
        print(f"Schema 文件: {schema_path}")
        print(f"条目数: {result.entries_checked}")
        print()

        # Category/Owner 汇总统计
        if result.category_summary:
            print("[INFO] 按分类(Category)汇总:")
            for category, count in sorted(result.category_summary.items()):
                print(f"  - {category}: {count} 条")
            print()

        if result.owner_summary and args.verbose:
            print("[INFO] 按负责人(Owner)汇总:")
            for owner, count in sorted(result.owner_summary.items()):
                print(f"  - {owner}: {count} 条")
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

        # 即将过期条目（默认显示）
        if result.expiring_soon_entries:
            print(
                f"[WARN] 即将过期的条目（{args.expiring_soon_days} 天内）: "
                f"{len(result.expiring_soon_entries)} 条"
            )
            for entry in result.expiring_soon_entries:
                print(
                    f"  - [{entry.entry_id}] {entry.expires_on} "
                    f"（{entry.days_until_expiry} 天后过期，owner: {entry.owner}）"
                )
            print()

        # 超过最大期限条目
        if result.exceeds_max_expiry_entries:
            severity = "ERROR" if args.fail_on_max_expiry else "WARN"
            print(
                f"[{severity}] 超过最大期限（{args.max_expiry_days} 天）的条目: "
                f"{len(result.exceeds_max_expiry_entries)} 条"
            )
            for entry in result.exceeds_max_expiry_entries:
                print(
                    f"  - [{entry.entry_id}] {entry.expires_on} "
                    f"（距离过期 {entry.days_until_expiry} 天，owner: {entry.owner}）"
                )
            print()

        # 其他警告
        other_warnings = [
            w for w in result.warnings
            if "即将过期" not in w.message and "超过" not in w.message
        ]
        if other_warnings and args.verbose:
            print(f"[WARN] 发现 {len(other_warnings)} 个其他警告:")
            for warn in other_warnings:
                print(f"  - [{warn.entry_id}] {warn.field}: {warn.message}")
            print()

        # 重复 id
        if result.duplicate_ids:
            print(f"[ERROR] 重复的 id: {result.duplicate_ids}")
            print()

        # 过期条目
        if result.expired_entries:
            print(f"[ERROR] 过期的条目: {result.expired_entries}")
            print()

        # 总结
        print("-" * 70)
        print(f"统计: 条目 {result.entries_checked} | "
              f"即将过期 {len(result.expiring_soon_entries)} | "
              f"超期限 {len(result.exceeds_max_expiry_entries)} | "
              f"已过期 {len(result.expired_entries)}")
        print()
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
