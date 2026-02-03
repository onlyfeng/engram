#!/usr/bin/env python3
"""更新 render_min_gate_block 的 Markdown fixtures。

用法:
    python scripts/iteration/update_render_min_gate_block_fixtures.py
    python scripts/iteration/update_render_min_gate_block_fixtures.py --iteration-number 13
    python scripts/iteration/update_render_min_gate_block_fixtures.py --profiles full regression
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, List

# 添加脚本目录到 path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from render_min_gate_block import SUPPORTED_PROFILES, GateProfile, render_min_gate_block  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "iteration" / "fixtures" / "render_min_gate_block"

DEFAULT_ITERATION_NUMBER = 13


def _validate_output(content: str, *, context: str) -> None:
    """稳定性校验（LF、末尾换行、禁止三连空行）。"""
    if "\r" in content:
        raise ValueError(f"{context}: 输出包含 CR 字符，请使用 LF")
    if not content.endswith("\n"):
        raise ValueError(f"{context}: 输出必须以换行符结尾")
    if "\n\n\n\n" in content:
        raise ValueError(f"{context}: 输出包含三连空行，请去除多余空行")


def _ensure_trailing_newline(content: str) -> str:
    if content.endswith("\n"):
        return content
    return content + "\n"


def update_fixtures(
    iteration_number: int = DEFAULT_ITERATION_NUMBER,
    output_dir: Path = FIXTURES_DIR,
    profiles: Iterable[GateProfile] | None = None,
) -> List[Path]:
    """更新 render_min_gate_block fixtures。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles_to_render = list(profiles or SUPPORTED_PROFILES)

    written: List[Path] = []
    for profile in profiles_to_render:
        content = render_min_gate_block(iteration_number, profile)
        content = _ensure_trailing_newline(content)
        _validate_output(content, context=f"render_min_gate_block:{profile}")

        path = output_dir / f"{profile}.md"
        path.write_text(content, encoding="utf-8")
        written.append(path)

    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        description="更新 render_min_gate_block 的 Markdown fixtures",
    )
    parser.add_argument(
        "--iteration-number",
        type=int,
        default=DEFAULT_ITERATION_NUMBER,
        help=f"迭代编号（默认: {DEFAULT_ITERATION_NUMBER}）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=FIXTURES_DIR,
        help=f"输出目录（默认: {FIXTURES_DIR}）",
    )
    parser.add_argument(
        "--profiles",
        nargs="*",
        default=None,
        help=f"指定 profile（默认: {', '.join(SUPPORTED_PROFILES)}）",
    )

    args = parser.parse_args()

    if args.profiles:
        unknown = [p for p in args.profiles if p not in SUPPORTED_PROFILES]
        if unknown:
            print(f"❌ 错误: 不支持的 profile: {', '.join(unknown)}", file=sys.stderr)
            return 1
        profiles = [p for p in args.profiles]
    else:
        profiles = None

    try:
        written = update_fixtures(
            iteration_number=args.iteration_number,
            output_dir=args.output_dir,
            profiles=profiles,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 显示错误信息
        print(f"❌ 错误: {exc}", file=sys.stderr)
        return 1

    for path in written:
        try:
            rel_path = path.relative_to(REPO_ROOT)
        except ValueError:
            rel_path = path
        print(f"[OK] 写入 {rel_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
