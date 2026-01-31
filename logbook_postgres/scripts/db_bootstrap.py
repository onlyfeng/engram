#!/usr/bin/env python3
"""
兼容入口: db_bootstrap.py
"""

import runpy
import sys
from pathlib import Path


def main() -> None:
    root_script = Path(__file__).resolve().parents[2] / "db_bootstrap.py"
    if not root_script.exists():
        print(f"[ERROR] 未找到根目录脚本: {root_script}", file=sys.stderr)
        sys.exit(1)
    runpy.run_path(str(root_script), run_name="__main__")


if __name__ == "__main__":
    main()
