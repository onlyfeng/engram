#!/usr/bin/env python3
"""迁移迭代证据到 v2 schema 并补齐必需字段。

用法:
    python scripts/iteration/migrate_iteration_evidence_to_v2.py [--dry-run]

功能:
    1. 扫描 docs/acceptance/evidence/*.json 的 canonical 证据文件
    2. 若 $schema 为 v1，则升级为 v2
    3. 若缺少 links.regression_doc_url，则按迭代号补齐
    4. 若缺少 source.source_path，则补齐为回归文档路径
    5. --dry-run 输出 diff 统计，不写入文件
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from scripts.iteration.iteration_evidence_naming import EVIDENCE_DIR, parse_evidence_filename
from scripts.iteration.iteration_evidence_schema import (
    CURRENT_SCHEMA_REF,
    LEGACY_SCHEMA_FILENAME,
    resolve_schema_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class MigrationOutcome:
    """单个文件迁移结果。"""

    path: Path
    changed: bool
    added_lines: int
    removed_lines: int
    warnings: list[str]


def default_regression_doc_path(iteration_number: int) -> str:
    """默认回归文档路径。"""

    return f"docs/acceptance/iteration_{iteration_number}_regression.md"


def is_legacy_schema(schema_value: Optional[str]) -> bool:
    """判断 $schema 是否为 v1。"""

    if not schema_value:
        return False
    return resolve_schema_name(schema_value) == LEGACY_SCHEMA_FILENAME


def insert_after_key(
    data: OrderedDict[str, Any],
    key: str,
    value: Any,
    after_keys: Iterable[str],
) -> OrderedDict[str, Any]:
    """在指定 key 之后插入新 key。"""

    if key in data:
        return data
    insert_after = None
    for candidate in after_keys:
        if candidate in data:
            insert_after = candidate
            break
    if insert_after is None:
        data[key] = value
        return data
    updated: OrderedDict[str, Any] = OrderedDict()
    for item_key, item_value in data.items():
        updated[item_key] = item_value
        if item_key == insert_after:
            updated[key] = value
    return updated


def ensure_links_regression(
    data: OrderedDict[str, Any],
    regression_doc_path: str,
) -> tuple[OrderedDict[str, Any], bool, list[str]]:
    """确保 links.regression_doc_url 存在。"""

    warnings: list[str] = []
    links = data.get("links")
    if links is None:
        links = OrderedDict()
        links["regression_doc_url"] = regression_doc_path
        data = insert_after_key(
            data,
            "links",
            links,
            ("sensitive_data_declaration", "overall_result", "commands"),
        )
        return data, True, warnings
    if not isinstance(links, dict):
        warnings.append("links 字段不是对象，已跳过补齐 regression_doc_url")
        return data, False, warnings
    if not isinstance(links, OrderedDict):
        links = OrderedDict(links)
        data["links"] = links
    existing = links.get("regression_doc_url")
    if not isinstance(existing, str) or not existing.strip():
        links["regression_doc_url"] = regression_doc_path
        return data, True, warnings
    return data, False, warnings


def ensure_source_path(
    data: OrderedDict[str, Any],
    source_path: str,
) -> tuple[OrderedDict[str, Any], bool, list[str]]:
    """确保 source.source_path 存在。"""

    warnings: list[str] = []
    source = data.get("source")
    if source is None:
        source = OrderedDict()
        source["source_path"] = source_path
        data = insert_after_key(data, "source", source, ("runner",))
        return data, True, warnings
    if not isinstance(source, dict):
        warnings.append("source 字段不是对象，已跳过补齐 source_path")
        return data, False, warnings
    if not isinstance(source, OrderedDict):
        source = OrderedDict(source)
        data["source"] = source
    existing = source.get("source_path")
    if not isinstance(existing, str) or not existing.strip():
        source["source_path"] = source_path
        return data, True, warnings
    return data, False, warnings


def resolve_iteration_number(
    data: dict[str, Any],
    fallback: int,
) -> int:
    """解析 iteration_number，必要时回退到文件名。"""

    raw = data.get("iteration_number")
    if isinstance(raw, int) and raw >= 1:
        return raw
    return fallback


def resolve_regression_doc_path(data: dict[str, Any], fallback: str) -> str:
    """优先使用已有 links.regression_doc_url。"""

    links = data.get("links")
    if isinstance(links, dict):
        value = links.get("regression_doc_url")
        if isinstance(value, str) and value.strip():
            return value
    return fallback


def render_json(data: OrderedDict[str, Any]) -> str:
    """渲染 JSON 文本。"""

    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def diff_stats(old_text: str, new_text: str) -> tuple[int, int]:
    """计算 diff 的新增/删除行数。"""

    diff = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        lineterm="",
    )
    added = 0
    removed = 0
    for line in diff:
        if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def format_path(path: Path) -> str:
    """格式化输出路径（尽量使用相对路径）。"""

    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def migrate_record(
    data: OrderedDict[str, Any],
    iteration_number: int,
) -> tuple[OrderedDict[str, Any], bool, list[str]]:
    """对单个证据记录执行迁移。"""

    changed = False
    warnings: list[str] = []

    schema_value = data.get("$schema")
    if isinstance(schema_value, str) and is_legacy_schema(schema_value):
        data["$schema"] = CURRENT_SCHEMA_REF
        changed = True

    default_path = default_regression_doc_path(iteration_number)
    regression_path = resolve_regression_doc_path(data, default_path)

    data, links_changed, links_warnings = ensure_links_regression(data, default_path)
    warnings.extend(links_warnings)
    if links_changed:
        changed = True

    data, source_changed, source_warnings = ensure_source_path(data, regression_path)
    warnings.extend(source_warnings)
    if source_changed:
        changed = True

    return data, changed, warnings


def migrate_file(path: Path, iteration_number: int, *, dry_run: bool) -> MigrationOutcome:
    """迁移单个证据文件。"""

    raw_text = path.read_text(encoding="utf-8")
    data = json.loads(raw_text, object_pairs_hook=OrderedDict)
    if not isinstance(data, OrderedDict):
        raise ValueError("JSON 根对象必须为 object")

    normalized_iteration = resolve_iteration_number(data, iteration_number)
    migrated, changed, warnings = migrate_record(data, normalized_iteration)
    if not changed:
        return MigrationOutcome(path, False, 0, 0, warnings)

    new_text = render_json(migrated)
    added, removed = diff_stats(raw_text, new_text)

    if not dry_run:
        path.write_text(new_text, encoding="utf-8")

    return MigrationOutcome(path, True, added, removed, warnings)


def main() -> int:
    """CLI 入口。"""

    parser = argparse.ArgumentParser(
        description="迁移 iteration evidence 到 v2 schema 并补齐字段",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=EVIDENCE_DIR,
        help="证据目录（默认: docs/acceptance/evidence）",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="预览模式，输出 diff 统计，不写入文件",
    )
    args = parser.parse_args()

    evidence_dir: Path = args.evidence_dir
    if not evidence_dir.exists():
        print(f"❌ 证据目录不存在: {evidence_dir}", file=sys.stderr)
        return 1

    json_paths = sorted(evidence_dir.glob("*.json"))
    outcomes: list[MigrationOutcome] = []
    errors: list[str] = []

    for path in json_paths:
        try:
            parsed = parse_evidence_filename(path.name)
        except ValueError:
            continue
        if not parsed.get("is_canonical"):
            continue
        iteration_number = int(parsed["iteration_number"])
        try:
            outcome = migrate_file(path, iteration_number, dry_run=args.dry_run)
            outcomes.append(outcome)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")

    if errors:
        print("❌ 迁移过程中出现错误:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    changed_files = [o for o in outcomes if o.changed]
    added_total = sum(o.added_lines for o in changed_files)
    removed_total = sum(o.removed_lines for o in changed_files)

    if args.dry_run:
        if not changed_files:
            print("🔍 [DRY-RUN] 未发现需要迁移的文件")
            return 0
        for outcome in changed_files:
            rel_path = format_path(outcome.path)
            print(f"🔍 [DRY-RUN] {rel_path}: +{outcome.added_lines} -{outcome.removed_lines}")
        print()
        print(
            f"🔍 [DRY-RUN] 变更文件数: {len(changed_files)}，新增 {added_total} 行，删除 {removed_total} 行"
        )
        return 0

    if not changed_files:
        print("✅ 未发现需要迁移的文件")
        return 0

    for outcome in changed_files:
        rel_path = format_path(outcome.path)
        print(f"✅ 已更新: {rel_path}")
        for warning in outcome.warnings:
            print(f"⚠️  {rel_path}: {warning}")

    print()
    print(f"完成迁移: {len(changed_files)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
