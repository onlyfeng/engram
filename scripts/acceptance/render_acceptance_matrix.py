#!/usr/bin/env python3
"""
Render acceptance test matrix from individual run records.

Reads: .artifacts/acceptance-runs/*.json
Outputs:
  - .artifacts/acceptance-matrix.md   (Markdown summary table)
  - .artifacts/acceptance-matrix.json (Structured JSON for further processing)

Usage:
    python scripts/acceptance/render_acceptance_matrix.py [--limit N] [--output-dir DIR]

Options:
    --limit N        Show last N records per (name, profile, workflow) group (default: 5)
    --output-dir     Output directory (default: .artifacts)
    --json-only      Only output JSON, skip Markdown
    --md-only        Only output Markdown, skip JSON
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_timestamp(ts: str) -> datetime:
    """Parse ISO 8601 timestamp string to datetime."""
    # Handle both with and without timezone
    try:
        # Try with timezone
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        # Try without timezone
        return datetime.fromisoformat(ts)


def load_acceptance_runs(runs_dir: Path) -> list[dict[str, Any]]:
    """Load all acceptance run records from directory."""
    records = []

    if not runs_dir.exists():
        return records

    for path in sorted(runs_dir.glob("*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
                data["_source_file"] = str(path.name)
                records.append(data)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] Failed to load {path}: {e}", file=sys.stderr)

    return records


def get_group_key(record: dict[str, Any]) -> tuple[str, str, str]:
    """
    Extract grouping key from record.
    
    Returns: (name, profile, workflow)
    """
    name = record.get("name", "unknown")
    metadata = record.get("metadata", {})
    profile = metadata.get("profile", "default")
    workflow = metadata.get("workflow", "manual")
    return (name, profile, workflow)


def group_records(
    records: list[dict[str, Any]],
    limit: int = 5,
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """
    Group records by (name, profile, workflow) and keep last N per group.
    
    Records are sorted by timestamp descending within each group.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)

    for record in records:
        key = get_group_key(record)
        groups[key].append(record)

    # Sort each group by timestamp descending, keep last N
    for key in groups:
        groups[key] = sorted(
            groups[key],
            key=lambda r: r.get("timestamp", ""),
            reverse=True,
        )[:limit]

    return dict(groups)


def compute_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary statistics for a group of records."""
    if not records:
        return {"count": 0}

    pass_count = sum(1 for r in records if r.get("result") == "PASS")
    fail_count = sum(1 for r in records if r.get("result") == "FAIL")
    partial_count = sum(1 for r in records if r.get("result") == "PARTIAL")

    durations = [r["duration_seconds"] for r in records if "duration_seconds" in r]
    avg_duration = sum(durations) / len(durations) if durations else None

    latest = records[0] if records else None

    return {
        "count": len(records),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "partial_count": partial_count,
        "pass_rate": pass_count / len(records) if records else 0,
        "avg_duration_seconds": round(avg_duration, 1) if avg_duration else None,
        "latest_timestamp": latest.get("timestamp") if latest else None,
        "latest_result": latest.get("result") if latest else None,
        "latest_commit": latest.get("commit", "")[:8] if latest else None,
    }


def render_markdown(
    groups: dict[tuple[str, str, str], list[dict[str, Any]]],
    output_path: Path,
    limit: int,
) -> None:
    """Render acceptance matrix as Markdown."""
    lines = [
        "# 验收测试矩阵摘要（自动生成）",
        "",
        f"> 生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"> 每组显示最近 {limit} 条记录",
        "",
        "## 概览",
        "",
        "| Name | Profile | Workflow | Pass Rate | Latest | Commit | Avg Duration |",
        "|------|---------|----------|-----------|--------|--------|--------------|",
    ]

    # Sort groups by name, then workflow, then profile
    sorted_keys = sorted(groups.keys(), key=lambda k: (k[0], k[2], k[1]))

    for key in sorted_keys:
        name, profile, workflow = key
        records = groups[key]
        stats = compute_stats(records)

        # Format pass rate with emoji
        pass_rate = stats["pass_rate"]
        if pass_rate == 1.0:
            rate_str = "✅ 100%"
        elif pass_rate >= 0.8:
            rate_str = f"🟡 {int(pass_rate * 100)}%"
        elif pass_rate > 0:
            rate_str = f"🔴 {int(pass_rate * 100)}%"
        else:
            rate_str = "⚫ N/A"

        # Format latest result
        latest_result = stats.get("latest_result", "N/A")
        if latest_result == "PASS":
            result_str = "✅ PASS"
        elif latest_result == "FAIL":
            result_str = "❌ FAIL"
        elif latest_result == "PARTIAL":
            result_str = "🟡 PARTIAL"
        else:
            result_str = latest_result

        # Format duration
        avg_dur = stats.get("avg_duration_seconds")
        dur_str = f"{avg_dur}s" if avg_dur else "-"

        # Format commit
        commit_str = f"`{stats.get('latest_commit', '-')}`" if stats.get('latest_commit') else "-"

        lines.append(
            f"| {name} | {profile} | {workflow} | {rate_str} | {result_str} | {commit_str} | {dur_str} |"
        )

    lines.extend([
        "",
        "## 详细记录",
        "",
    ])

    # Detailed records per group
    for key in sorted_keys:
        name, profile, workflow = key
        records = groups[key]

        lines.extend([
            f"### {name} ({profile} / {workflow})",
            "",
            "| Timestamp | Result | Commit | Duration | Artifacts |",
            "|-----------|--------|--------|----------|-----------|",
        ])

        for record in records:
            ts = record.get("timestamp", "N/A")
            # Shorten timestamp for display
            if ts != "N/A":
                try:
                    dt = parse_timestamp(ts)
                    ts_short = dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    ts_short = ts[:16]
            else:
                ts_short = ts

            result = record.get("result", "N/A")
            commit = record.get("commit", "")[:8] if record.get("commit") else "-"
            duration = record.get("duration_seconds")
            dur_str = f"{duration}s" if duration else "-"

            artifacts_dir = record.get("artifacts_dir", "-")
            artifacts_count = len(record.get("artifacts", []))
            artifacts_str = f"`{artifacts_dir}` ({artifacts_count} files)" if artifacts_dir != "-" else "-"

            # Result with emoji
            if result == "PASS":
                result_str = "✅ PASS"
            elif result == "FAIL":
                result_str = "❌ FAIL"
            elif result == "PARTIAL":
                result_str = "🟡 PARTIAL"
            else:
                result_str = result

            lines.append(
                f"| {ts_short} | {result_str} | `{commit}` | {dur_str} | {artifacts_str} |"
            )

        lines.append("")

    lines.extend([
        "---",
        "",
        "*此文件由 `scripts/acceptance/render_acceptance_matrix.py` 自动生成*",
        "",
        "查看使用说明: [docs/acceptance/00_acceptance_matrix.md](../../docs/acceptance/00_acceptance_matrix.md#自动汇总产物)",
    ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write("\n".join(lines))
        f.write("\n")

    print(f"[OK] Markdown matrix written to: {output_path}")


def render_json(
    groups: dict[tuple[str, str, str], list[dict[str, Any]]],
    output_path: Path,
    limit: int,
) -> None:
    """Render acceptance matrix as JSON."""
    output: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "limit_per_group": limit,
        "groups": [],
    }

    # Sort groups by name, then workflow, then profile
    sorted_keys = sorted(groups.keys(), key=lambda k: (k[0], k[2], k[1]))

    for key in sorted_keys:
        name, profile, workflow = key
        records = groups[key]
        stats = compute_stats(records)

        group_entry = {
            "name": name,
            "profile": profile,
            "workflow": workflow,
            "stats": stats,
            "records": [
                {
                    "timestamp": r.get("timestamp"),
                    "result": r.get("result"),
                    "commit": r.get("commit"),
                    "duration_seconds": r.get("duration_seconds"),
                    "artifacts_dir": r.get("artifacts_dir"),
                    "artifacts_count": len(r.get("artifacts", [])),
                    "metadata": r.get("metadata"),
                    "source_file": r.get("_source_file"),
                }
                for r in records
            ],
        }
        output["groups"].append(group_entry)

    # Summary stats
    all_records = [r for records in groups.values() for r in records]
    output["summary"] = {
        "total_groups": len(groups),
        "total_records": len(all_records),
        "overall_pass_rate": (
            sum(1 for r in all_records if r.get("result") == "PASS") / len(all_records)
            if all_records else 0
        ),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"[OK] JSON matrix written to: {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render acceptance test matrix from run records",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Show last N records per (name, profile, workflow) group (default: 5)",
    )
    parser.add_argument(
        "--output-dir",
        default=".artifacts",
        help="Output directory (default: .artifacts)",
    )
    parser.add_argument(
        "--runs-dir",
        default=".artifacts/acceptance-runs",
        help="Directory containing acceptance run JSON files (default: .artifacts/acceptance-runs)",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Only output JSON, skip Markdown",
    )
    parser.add_argument(
        "--md-only",
        action="store_true",
        help="Only output Markdown, skip JSON",
    )

    args = parser.parse_args()

    if args.json_only and args.md_only:
        print("Error: --json-only and --md-only are mutually exclusive", file=sys.stderr)
        return 1

    runs_dir = Path(args.runs_dir)
    output_dir = Path(args.output_dir)

    # Load records
    records = load_acceptance_runs(runs_dir)

    if not records:
        print(f"[INFO] No acceptance run records found in {runs_dir}")
        print("[INFO] Generating empty matrix files...")

        # Still generate empty files for CI artifact consistency
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    else:
        print(f"[INFO] Loaded {len(records)} acceptance run records")
        groups = group_records(records, limit=args.limit)
        print(f"[INFO] Grouped into {len(groups)} groups")

    # Render outputs
    if not args.json_only:
        md_path = output_dir / "acceptance-matrix.md"
        render_markdown(groups, md_path, args.limit)

    if not args.md_only:
        json_path = output_dir / "acceptance-matrix.json"
        render_json(groups, json_path, args.limit)

    return 0


if __name__ == "__main__":
    sys.exit(main())
