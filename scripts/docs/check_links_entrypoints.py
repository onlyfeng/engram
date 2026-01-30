#!/usr/bin/env python3
"""
入口文件链接检查脚本（轻量版）

专门扫描 README.md 系列入口文件的链接有效性：
- README.md（项目根目录）
- docs/README.md
- docs/*/README.md
- apps/*/README.md
- apps/*/docs/README.md

这是 check_links.py 的轻量入口，等效于：
    python check_links.py --entrypoints

用法：
    python check_links_entrypoints.py
    python check_links_entrypoints.py --output ./artifacts
    python check_links_entrypoints.py --verbose
"""

import sys
from pathlib import Path

# 导入主检查模块
sys.path.insert(0, str(Path(__file__).parent))
from check_links import (
    PROJECT_ROOT,
    DEFAULT_OUTPUT_DIR,
    BUILTIN_IGNORE_PATTERNS,
    LinkReport,
    discover_entrypoint_files,
    scan_file,
)

import argparse
import json


def main():
    parser = argparse.ArgumentParser(
        description="检查入口文件（README.md 系列）中的本地链接有效性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
扫描的入口文件：
  - README.md（项目根目录）
  - docs/README.md
  - docs/*/README.md
  - apps/*/README.md
  - apps/*/docs/README.md

这是 check_links.py 的轻量入口，仅扫描入口文件。
如需扫描完整文档目录，请使用：python check_links.py
        """
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录（默认: {DEFAULT_OUTPUT_DIR}）"
    )
    parser.add_argument(
        "--project-root", "-r",
        default=str(PROJECT_ROOT),
        help="项目根目录（默认: 自动检测）"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细输出"
    )
    parser.add_argument(
        "--ignore-patterns", "-i",
        nargs="*",
        default=[],
        help="要忽略的路径模式列表"
    )
    
    args = parser.parse_args()
    
    project_root = Path(args.project_root).resolve()
    ignore_patterns = set(args.ignore_patterns) | set(BUILTIN_IGNORE_PATTERNS)
    
    # 发现入口文件
    entrypoint_paths = discover_entrypoint_files(project_root)
    scan_files = [str(p.relative_to(project_root)) for p in entrypoint_paths]
    
    print(f"项目根目录: {project_root}")
    print(f"入口文件数: {len(scan_files)}")
    for f in scan_files:
        print(f"  - {f}")
    print()
    
    # 初始化报告
    report = LinkReport(
        scan_files=scan_files,
        ignored_patterns=list(ignore_patterns)
    )
    
    # 扫描所有入口文件
    for rel_file in scan_files:
        file_path = project_root / rel_file
        print(f"扫描: {rel_file}...")
        
        checked_count, broken, source_type = scan_file(
            file_path, project_root, ignore_patterns, source_type="entrypoint"
        )
        
        if source_type is not None:
            report.files_scanned += 1
            report.total_links_checked += checked_count
            report.broken_links.extend(broken)
            report.entrypoint_files_scanned += 1
            
            if args.verbose:
                print(f"  - 链接数: {checked_count}")
                print(f"  - 失效数: {len(broken)}")
    
    # 统计失效链接
    report.entrypoint_broken_count = len(report.broken_links)
    
    # 确保输出目录存在
    output_dir = project_root / args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 写入报告
    report_path = output_dir / "entrypoints_link_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    
    # 输出摘要
    print()
    print("=" * 50)
    print("入口文件链接检查报告")
    print("=" * 50)
    print(f"扫描入口文件数: {report.entrypoint_files_scanned}")
    print(f"检查链接数: {report.total_links_checked}")
    print(f"失效链接数: {report.entrypoint_broken_count}")
    print(f"报告路径: {report_path}")
    
    if report.broken_links:
        print()
        print("失效链接列表:")
        print("-" * 50)
        for link in report.broken_links:
            print(f"  [{link.link_type}] {link.source_file}:{link.line_number}")
            print(f"    目标: {link.target_path}")
            print(f"    原因: {link.reason}")
        
        sys.exit(1)
    else:
        print()
        print("所有入口文件链接均有效！")
        sys.exit(0)


if __name__ == "__main__":
    main()
