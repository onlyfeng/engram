#!/usr/bin/env python3
"""
import_copy.py - Engram 项目导入复制工具

功能:
  1. 读取 manifest v1 文件，自动复制所需文件
  2. 支持 --src/--dst/--dry-run 参数
  3. 支持 --mode logbook-only|unified 选择部署模式
  4. 复制完成后自动运行 import_preflight.py 验证

用法:
  # 基本用法（默认 unified 模式）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target

  # Logbook-only 模式（轻量级事实账本）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --mode logbook-only

  # Unified 模式（完整栈）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --mode unified

  # 干运行（仅显示将要复制的文件）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --dry-run

  # 使用自定义 manifest（覆盖 --mode 选择）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --manifest custom.json

  # 包含可选文件（如 SeekDB）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --include-optional

  # 跳过预检
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --skip-preflight

退出码:
  0 - 成功
  1 - 复制失败或预检失败
  2 - 参数错误
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# ANSI 颜色
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"  # No Color

# 部署模式与 manifest 路径映射（相对于 engram 源码根目录）
MODE_MANIFEST_MAP = {
    "unified": "docs/guides/manifests/unified_stack_import_v1.json",
    "logbook-only": "docs/guides/manifests/logbook_only_import_v1.json",
}

# 默认模式
DEFAULT_MODE = "unified"


def log_info(msg: str) -> None:
    """打印信息"""
    print(f"{BLUE}[INFO]{NC} {msg}")


def log_ok(msg: str) -> None:
    """打印成功"""
    print(f"{GREEN}[OK]{NC} {msg}")


def log_warn(msg: str) -> None:
    """打印警告"""
    print(f"{YELLOW}[WARN]{NC} {msg}")


def log_error(msg: str) -> None:
    """打印错误"""
    print(f"{RED}[ERROR]{NC} {msg}")


def log_dry_run(msg: str) -> None:
    """打印干运行信息"""
    print(f"{YELLOW}[DRY-RUN]{NC} {msg}")


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    """加载 manifest 文件"""
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest 文件不存在: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def copy_file(src: Path, dst: Path, dry_run: bool = False) -> bool:
    """复制单个文件"""
    if dry_run:
        log_dry_run(f"复制文件: {src} -> {dst}")
        return True

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except Exception as e:
        log_error(f"复制文件失败: {src} -> {dst}: {e}")
        return False


def copy_directory(src: Path, dst: Path, dry_run: bool = False) -> bool:
    """复制目录"""
    if dry_run:
        log_dry_run(f"复制目录: {src} -> {dst}")
        return True

    try:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        return True
    except Exception as e:
        log_error(f"复制目录失败: {src} -> {dst}: {e}")
        return False


def process_file_entry(
    entry: dict[str, Any],
    src_root: Path,
    dst_root: Path,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    处理单个文件条目

    返回: (成功数, 失败数)
    """
    entry_id = entry.get("id", "unknown")
    entry_type = entry.get("type", "file")
    description = entry.get("description", "")

    success_count = 0
    fail_count = 0

    if entry_type == "file":
        # 单文件
        source_path = entry.get("source_path", "")
        target_path = entry.get("target_path", source_path)

        src = src_root / source_path
        dst = dst_root / target_path

        if not src.exists():
            log_warn(f"源文件不存在，跳过: {source_path} ({entry_id})")
            return (0, 1)

        log_info(f"复制 [{entry_id}]: {description}")
        if copy_file(src, dst, dry_run):
            success_count += 1
        else:
            fail_count += 1

    elif entry_type == "directory":
        # 目录
        source_path = entry.get("source_path", "")
        target_path = entry.get("target_path", source_path)

        src = src_root / source_path
        dst = dst_root / target_path

        if not src.exists():
            log_warn(f"源目录不存在，跳过: {source_path} ({entry_id})")
            return (0, 1)

        log_info(f"复制 [{entry_id}]: {description}")
        if copy_directory(src, dst, dry_run):
            success_count += 1
        else:
            fail_count += 1

    elif entry_type == "files":
        # 多文件
        source_paths = entry.get("source_paths", [])
        target_paths = entry.get("target_paths", source_paths)

        log_info(f"复制 [{entry_id}]: {description}")

        for source_path, target_path in zip(source_paths, target_paths):
            src = src_root / source_path
            dst = dst_root / target_path

            if not src.exists():
                log_warn(f"  源文件不存在，跳过: {source_path}")
                fail_count += 1
                continue

            if copy_file(src, dst, dry_run):
                success_count += 1
            else:
                fail_count += 1

    return (success_count, fail_count)


def copy_from_manifest(
    manifest: dict[str, Any],
    src_root: Path,
    dst_root: Path,
    include_optional: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    根据 manifest 复制文件

    返回: 包含统计信息的字典
        {
            "required_success": int,
            "required_fail": int,
            "optional_success": int,
            "optional_fail": int,
            "optional_skipped": int,
            "required_entries": list[str],
            "optional_entries": list[str],
        }
    """
    files_section = manifest.get("files", {})
    required_files = files_section.get("required", [])
    optional_files = files_section.get("optional", [])

    stats = {
        "required_success": 0,
        "required_fail": 0,
        "optional_success": 0,
        "optional_fail": 0,
        "optional_skipped": len(optional_files) if not include_optional else 0,
        "required_entries": [e.get("id", "unknown") for e in required_files],
        "optional_entries": [e.get("id", "unknown") for e in optional_files],
    }

    # 处理必需文件
    print()
    print("=" * 50)
    print("复制必需文件")
    print("=" * 50)

    for entry in required_files:
        success, fail = process_file_entry(entry, src_root, dst_root, dry_run)
        stats["required_success"] += success
        stats["required_fail"] += fail

    # 处理可选文件
    if include_optional and optional_files:
        print()
        print("=" * 50)
        print("复制可选文件")
        print("=" * 50)

        for entry in optional_files:
            success, fail = process_file_entry(entry, src_root, dst_root, dry_run)
            stats["optional_success"] += success
            stats["optional_fail"] += fail

    return stats


def run_preflight(
    dst_root: Path,
    src_root: Path,
    verbose: bool = False,
) -> bool:
    """运行 import_preflight.py 进行验证"""
    preflight_script = src_root / "scripts" / "import_preflight.py"

    if not preflight_script.exists():
        log_warn(f"预检脚本不存在: {preflight_script}")
        return False

    log_info("运行导入预检...")
    print()

    cmd = [sys.executable, str(preflight_script), str(dst_root)]
    if verbose:
        cmd.append("--verbose")

    try:
        result = subprocess.run(cmd, cwd=str(dst_root))
        return result.returncode == 0
    except Exception as e:
        log_error(f"运行预检脚本失败: {e}")
        return False


def main() -> int:
    """主入口"""
    parser = argparse.ArgumentParser(
        description="Engram 项目导入复制工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基本用法（默认 unified 模式）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target

  # Logbook-only 模式（轻量级事实账本）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --mode logbook-only

  # Unified 模式（完整栈）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --mode unified

  # 干运行
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --dry-run

  # 包含可选文件
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --include-optional

  # 使用自定义 manifest（覆盖 --mode）
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --manifest custom.json

  # 跳过预检
  python scripts/import_copy.py --src /path/to/engram --dst /path/to/target --skip-preflight
""",
    )

    parser.add_argument(
        "--src",
        type=Path,
        required=True,
        help="Engram 源码根目录路径",
    )
    parser.add_argument(
        "--dst",
        type=Path,
        required=True,
        help="目标项目根目录路径",
    )
    parser.add_argument(
        "--mode",
        choices=["logbook-only", "unified"],
        default=DEFAULT_MODE,
        help="部署模式: logbook-only（轻量级事实账本）或 unified（完整栈，默认）",
    )
    parser.add_argument(
        "--manifest",
        "-m",
        type=Path,
        default=None,
        help="Manifest 文件路径（覆盖 --mode 的默认选择）",
    )
    parser.add_argument(
        "--include-optional",
        action="store_true",
        help="包含可选文件（如 SeekDB SQL、Gateway 模板等）",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="干运行模式，仅显示将要复制的文件，不实际复制",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="跳过复制后的预检验证",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细输出",
    )

    args = parser.parse_args()

    # 验证源目录
    src_root = args.src.resolve()
    if not src_root.exists():
        log_error(f"源目录不存在: {src_root}")
        return 2

    if not src_root.is_dir():
        log_error(f"源路径不是目录: {src_root}")
        return 2

    # 验证目标目录
    dst_root = args.dst.resolve()
    if not dst_root.exists():
        if args.dry_run:
            log_dry_run(f"目标目录将被创建: {dst_root}")
        else:
            dst_root.mkdir(parents=True, exist_ok=True)
            log_info(f"创建目标目录: {dst_root}")
    elif not dst_root.is_dir():
        log_error(f"目标路径不是目录: {dst_root}")
        return 2

    # 加载 manifest
    manifest_source = ""  # 用于记录 manifest 来源
    if args.manifest:
        manifest_path = args.manifest.resolve()
        manifest_source = "custom"
    else:
        manifest_path = src_root / MODE_MANIFEST_MAP[args.mode]
        manifest_source = args.mode

    try:
        manifest = load_manifest(manifest_path)
    except FileNotFoundError as e:
        log_error(str(e))
        return 2
    except json.JSONDecodeError as e:
        log_error(f"Manifest JSON 解析失败: {e}")
        return 2

    manifest_version = manifest.get("manifest_version", "unknown")
    manifest_title = manifest.get("title", "Unknown Manifest")

    # 打印标题
    print("=" * 60)
    print("Engram 项目导入复制工具")
    print("=" * 60)
    print(f"源目录: {src_root}")
    print(f"目标目录: {dst_root}")
    print(f"部署模式: {args.mode}")
    print(f"Manifest: {manifest_path.name} (v{manifest_version})")
    if manifest_source == "custom":
        print(f"{YELLOW}注意: 使用自定义 manifest，--mode 参数被覆盖{NC}")
    if args.dry_run:
        print(f"{YELLOW}模式: 干运行（不实际复制）{NC}")
    if args.include_optional:
        print(f"{BLUE}包含可选文件: 是{NC}")
    else:
        print(f"{BLUE}包含可选文件: 否（使用 --include-optional 启用）{NC}")

    # 执行复制
    stats = copy_from_manifest(
        manifest=manifest,
        src_root=src_root,
        dst_root=dst_root,
        include_optional=args.include_optional,
        dry_run=args.dry_run,
    )

    # 计算总数
    total_success = stats["required_success"] + stats["optional_success"]
    total_fail = stats["required_fail"] + stats["optional_fail"]

    # 打印复制结果
    print()
    print("=" * 50)
    print("复制结果")
    print("=" * 50)

    # 显示 required/optional 选择结果
    print()
    print(f"{BLUE}[Required 文件]{NC}")
    print(f"  条目: {', '.join(stats['required_entries']) or '无'}")
    if args.dry_run:
        print(f"  状态: 将复制 {stats['required_success']} 个")
    else:
        print(f"  状态: 成功 {stats['required_success']}，失败 {stats['required_fail']}")

    print()
    print(f"{BLUE}[Optional 文件]{NC}")
    print(f"  条目: {', '.join(stats['optional_entries']) or '无'}")
    if args.include_optional:
        if args.dry_run:
            print(f"  状态: 将复制 {stats['optional_success']} 个")
        else:
            print(f"  状态: 成功 {stats['optional_success']}，失败 {stats['optional_fail']}")
    else:
        print(f"  状态: {YELLOW}跳过（共 {stats['optional_skipped']} 个条目，使用 --include-optional 启用）{NC}")

    print()
    if args.dry_run:
        print(f"将复制 {total_success} 个文件/目录")
        if total_fail > 0:
            print(f"{YELLOW}警告: {total_fail} 个源文件/目录不存在{NC}")
    else:
        if total_fail == 0:
            log_ok(f"成功复制 {total_success} 个文件/目录")
        else:
            log_warn(f"复制完成: 成功 {total_success}，失败 {total_fail}")

    # 运行预检
    if not args.dry_run and not args.skip_preflight:
        print()
        print("=" * 50)
        print("运行导入预检")
        print("=" * 50)

        if run_preflight(dst_root, src_root, args.verbose):
            log_ok("预检通过")
        else:
            log_error("预检失败，请检查上述错误")
            return 1

    # 最终结果
    print()
    if args.dry_run:
        print(f"{YELLOW}干运行完成，未实际复制任何文件{NC}")
        return 0
    elif total_fail > 0:
        return 1
    else:
        print(f"{GREEN}导入完成！{NC}")
        print()
        print("后续步骤:")
        print(f"  1. 创建环境变量文件: cd {dst_root} && touch .env")
        if args.mode == "logbook-only":
            # Logbook-only 模式的后续步骤
            print("  2. 配置必需变量:")
            print("       PROJECT_KEY=myproject")
            print("       POSTGRES_DB=myproject")
            print("       POSTGRES_PASSWORD=<secure_password>")
            print()
            print("     [可选] 服务账号模式（最小权限）:")
            print("       LOGBOOK_MIGRATOR_PASSWORD=<secure_password>")
            print("       LOGBOOK_SVC_PASSWORD=<secure_password>")
            print("  3. 启动服务:")
            print("       docker compose -f compose/logbook.yml up -d")
            print()
            print("验证服务:")
            print("       # 使用 postgres 超级用户")
            print("       docker compose -f compose/logbook.yml exec postgres pg_isready -U postgres")
            print()
            print("       # 或使用 logbook health（需安装 engram_logbook 包）")
            print("       POSTGRES_DSN=\"postgresql://postgres:$POSTGRES_PASSWORD@localhost:5432/$POSTGRES_DB\" logbook health")
        else:
            # Unified 模式的后续步骤
            print("  2. 配置必需变量:")
            print("       PROJECT_KEY=myproject")
            print("       POSTGRES_DB=myproject")
            print("       LOGBOOK_MIGRATOR_PASSWORD=<secure_password>")
            print("       LOGBOOK_SVC_PASSWORD=<secure_password>")
            print("       OPENMEMORY_MIGRATOR_PASSWORD=<secure_password>")
            print("       OPENMEMORY_SVC_PASSWORD=<secure_password>")
            print("  3. 启动服务:")
            print("       docker compose -f docker-compose.engram.yml up -d")
            print()
            print("验证服务:")
            print("       curl http://localhost:8080/health   # OpenMemory")
            print("       curl http://localhost:8787/health   # Gateway")
        print()
        print("参考文档: docs/guides/integrate_existing_project.md")
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
