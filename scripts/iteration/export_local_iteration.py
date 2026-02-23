#!/usr/bin/env python3
"""导出本地迭代草稿以便分享或存档。

用法:
    python scripts/iteration/export_local_iteration.py <iteration_number> [options]

示例:
    # 输出到 stdout（便于复制粘贴）
    python scripts/iteration/export_local_iteration.py 13

    # 输出到目录
    python scripts/iteration/export_local_iteration.py 13 --output-dir .artifacts/iteration-draft-export/iteration_13/

    # 打包为 zip（推荐用于分享）
    python scripts/iteration/export_local_iteration.py 13 --output-zip .artifacts/iteration_13_draft.zip

功能:
    1. 读取 .iteration/<N>/plan.md 和 .iteration/<N>/regression.md
    2. 默认输出到 stdout，便于复制粘贴分享
    3. 可选输出到指定目录（--output-dir）
    4. 可选打包为 zip 文件（--output-zip，推荐用于分享）
    5. 输出内容包含明确的"非 SSOT"声明和下一步指令
    6. 检测并警告草稿中的 .iteration/ 链接（建议改为文本/inline code）

警告:
    本脚本导出的内容来源于本地草稿（.iteration/），不是 SSOT。
    导出内容不应直接链接或引用 .iteration/ 路径。
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 本地迭代目录
ITERATION_DIR = REPO_ROOT / ".iteration"

# 兼容历史测试/脚本通过扁平模块名 monkeypatch（如 "export_local_iteration.ITERATION_DIR"）。
sys.modules.setdefault("export_local_iteration", sys.modules[__name__])


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class IterationLinkWarning:
    """草稿中检测到的 .iteration/ 链接警告。"""

    file_name: str
    line_number: int
    line_content: str
    link_text: str


@dataclass
class ExportResult:
    """导出操作结果。"""

    success: bool
    message: str
    plan_content: Optional[str]
    regression_content: Optional[str]
    warnings: List[IterationLinkWarning]
    output_files: List[str] = field(default_factory=list)
    zip_path: Optional[str] = None


class SourceNotFoundError(Exception):
    """当源文件不存在时抛出。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"源文件不存在: {path}")


# ============================================================================
# 链接检测
# ============================================================================

# 检测 Markdown 链接中的 .iteration/ 路径
# 匹配模式: [text](.../.iteration/...) 或 [text](.iteration/...)
ITERATION_LINK_PATTERN = re.compile(
    r"\[([^\]]*)\]\(([^)]*\.iteration[^)]*)\)",
    re.IGNORECASE,
)


def detect_iteration_links(content: str, file_name: str) -> List[IterationLinkWarning]:
    """检测内容中的 .iteration/ 链接。

    Args:
        content: 文件内容
        file_name: 文件名（用于报告）

    Returns:
        检测到的警告列表
    """
    warnings: List[IterationLinkWarning] = []

    for line_number, line in enumerate(content.splitlines(), start=1):
        for match in ITERATION_LINK_PATTERN.finditer(line):
            warnings.append(
                IterationLinkWarning(
                    file_name=file_name,
                    line_number=line_number,
                    line_content=line.strip(),
                    link_text=match.group(0),
                )
            )

    return warnings


# ============================================================================
# 导出声明模板
# ============================================================================


def get_export_header(iteration_number: int) -> str:
    """生成导出文件头部声明。

    Args:
        iteration_number: 迭代编号

    Returns:
        头部声明文本
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""> **⚠️ 非 SSOT - 本地草稿导出**
>
> 本内容来源于本地迭代草稿目录 `.iteration/{iteration_number}/`，**不是权威版本**（SSOT）。
>
> - 导出时间: {timestamp}
> - 来源路径: `.iteration/{iteration_number}/`（本地草稿，不应链接）
> - 状态: 草稿，未晋升到 `docs/acceptance/`
>
> **请勿在版本化文档中链接 `.iteration/` 路径。**

---

"""


def get_export_footer(iteration_number: int) -> str:
    """生成导出文件尾部的下一步指令。

    Args:
        iteration_number: 迭代编号

    Returns:
        尾部指令文本
    """
    return f"""
---

## 下一步操作

### 1. 晋升到 SSOT（如果计划已成熟）

```bash
# 预览晋升操作
python scripts/iteration/promote_iteration.py {iteration_number} --dry-run

# 执行晋升
python scripts/iteration/promote_iteration.py {iteration_number}

# 如需标记旧迭代为已取代
python scripts/iteration/promote_iteration.py {iteration_number} --supersede <OLD_N>
```

### 2. 运行门禁检查

```bash
# 完整 CI 检查
make ci

# 特定检查
make check-iteration-docs  # 确保无违规 .iteration/ 链接
make check-iteration-docs-superseded-only  # 仅检查 SUPERSEDED 一致性（快速验证）
```

### 3. 注意事项

- **不要链接 `.iteration/`**: 版本化文档（`docs/`）中不应包含指向 `.iteration/` 的链接
- **晋升后路径变化**: 晋升后文件路径为 `docs/acceptance/iteration_{iteration_number}_plan.md` 和 `docs/acceptance/iteration_{iteration_number}_regression.md`
- **使用文本引用**: 如需引用本地草稿，使用纯文本或 inline code（如 `.iteration/{iteration_number}/`）而非 Markdown 链接
"""


# ============================================================================
# ZIP 导出 README 模板
# ============================================================================


def get_zip_readme_content(iteration_number: int) -> str:
    """生成 zip 包中的 README 内容。

    Args:
        iteration_number: 迭代编号

    Returns:
        README 文本内容
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""# Iteration {iteration_number} 草稿导出包

> **⚠️ 非 SSOT - 本地草稿导出**
>
> 本包内容来源于本地迭代草稿目录 `.iteration/{iteration_number}/`，**不是权威版本**（SSOT）。

## 包内容

- `plan.md` - 迭代计划草稿
- `regression.md` - 回归记录草稿
- `README.md` - 本说明文件

## 元数据

- **导出时间**: {timestamp}
- **来源路径**: `.iteration/{iteration_number}/`（本地草稿，不应链接）
- **状态**: 草稿，未晋升到 `docs/acceptance/`

## 使用说明

### 1. 查阅内容

直接打开 `plan.md` 和 `regression.md` 查看迭代计划和回归记录草稿。

### 2. 提供反馈

如需提供反馈，请通过 Slack / 邮件等渠道与作者沟通。

### 3. 晋升到 SSOT

若计划已成熟，作者应使用以下命令晋升到 SSOT：

```bash
# 预览晋升操作
python scripts/iteration/promote_iteration.py {iteration_number} --dry-run

# 执行晋升
python scripts/iteration/promote_iteration.py {iteration_number}
```

## 注意事项

- **请勿在版本化文档中链接 `.iteration/` 路径**
- 晋升后文件路径为 `docs/acceptance/iteration_{iteration_number}_plan.md` 和 `docs/acceptance/iteration_{iteration_number}_regression.md`
"""


# ============================================================================
# 核心导出逻辑
# ============================================================================


def export_iteration(
    iteration_number: int,
    *,
    output_dir: Optional[Path] = None,
) -> ExportResult:
    """导出本地迭代草稿。

    Args:
        iteration_number: 要导出的迭代编号
        output_dir: 输出目录（None 表示输出到 stdout）

    Returns:
        ExportResult 操作结果

    Raises:
        SourceNotFoundError: 如果源目录或文件不存在
    """
    # 源目录
    src_dir = ITERATION_DIR / str(iteration_number)
    src_plan = src_dir / "plan.md"
    src_regression = src_dir / "regression.md"

    # 检查源目录是否存在
    if not src_dir.exists():
        raise SourceNotFoundError(src_dir)

    # 至少需要一个文件存在
    if not src_plan.exists() and not src_regression.exists():
        raise SourceNotFoundError(src_dir)

    # 读取文件内容
    plan_content: Optional[str] = None
    regression_content: Optional[str] = None
    all_warnings: List[IterationLinkWarning] = []

    if src_plan.exists():
        plan_content = src_plan.read_text(encoding="utf-8")
        all_warnings.extend(detect_iteration_links(plan_content, "plan.md"))

    if src_regression.exists():
        regression_content = src_regression.read_text(encoding="utf-8")
        all_warnings.extend(detect_iteration_links(regression_content, "regression.md"))

    # 生成导出内容
    header = get_export_header(iteration_number)
    footer = get_export_footer(iteration_number)

    output_files: List[str] = []

    if output_dir is not None:
        # 输出到文件
        output_dir.mkdir(parents=True, exist_ok=True)

        if plan_content is not None:
            plan_output = output_dir / "plan.md"
            plan_output.write_text(header + plan_content + footer, encoding="utf-8")
            output_files.append(str(plan_output))

        if regression_content is not None:
            regression_output = output_dir / "regression.md"
            regression_output.write_text(header + regression_content + footer, encoding="utf-8")
            output_files.append(str(regression_output))

    return ExportResult(
        success=True,
        message=f"Iteration {iteration_number} 草稿导出完成",
        plan_content=header + plan_content + footer if plan_content else None,
        regression_content=(header + regression_content + footer if regression_content else None),
        warnings=all_warnings,
        output_files=output_files,
    )


def export_iteration_zip(
    iteration_number: int,
    *,
    output_zip: Path,
) -> ExportResult:
    """导出本地迭代草稿为 zip 包。

    Args:
        iteration_number: 要导出的迭代编号
        output_zip: 输出 zip 文件路径

    Returns:
        ExportResult 操作结果

    Raises:
        SourceNotFoundError: 如果源目录或文件不存在
    """
    # 先调用普通导出获取内容
    result = export_iteration(iteration_number)

    if not result.success:
        return result

    # 创建输出目录（如果不存在）
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    # 创建 zip 文件
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        # 添加 README
        readme_content = get_zip_readme_content(iteration_number)
        zf.writestr("README.md", readme_content.encode("utf-8"))

        # 添加 plan.md（如果存在）
        if result.plan_content is not None:
            zf.writestr("plan.md", result.plan_content.encode("utf-8"))

        # 添加 regression.md（如果存在）
        if result.regression_content is not None:
            zf.writestr("regression.md", result.regression_content.encode("utf-8"))

    return ExportResult(
        success=True,
        message=f"Iteration {iteration_number} 草稿已打包为 zip",
        plan_content=result.plan_content,
        regression_content=result.regression_content,
        warnings=result.warnings,
        output_files=[],
        zip_path=str(output_zip),
    )


def format_warnings(warnings: List[IterationLinkWarning]) -> str:
    """格式化警告信息。

    Args:
        warnings: 警告列表

    Returns:
        格式化的警告文本
    """
    if not warnings:
        return ""

    lines = [
        "",
        "⚠️  检测到草稿中存在 .iteration/ 链接",
        "    建议改为纯文本或 inline code，避免分享内容诱导违规链接写入版本化文档。",
        "",
    ]

    for w in warnings:
        lines.append(f"    [{w.file_name}:{w.line_number}] {w.link_text}")

    lines.append("")
    return "\n".join(lines)


# ============================================================================
# CLI 入口
# ============================================================================


def main() -> int:
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="导出本地迭代草稿以便分享或存档",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 输出到 stdout（便于复制粘贴）
    python scripts/iteration/export_local_iteration.py 13

    # 输出到目录
    python scripts/iteration/export_local_iteration.py 13 --output-dir .artifacts/iteration-draft-export/iteration_13/

    # 打包为 zip（推荐用于分享）
    python scripts/iteration/export_local_iteration.py 13 --output-zip .artifacts/iteration_13_draft.zip

注意:
    导出内容来源于本地草稿，不是 SSOT。
    请勿在版本化文档中链接 .iteration/ 路径。
        """,
    )
    parser.add_argument(
        "iteration_number",
        type=int,
        help="要导出的迭代编号",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default=None,
        help="输出目录（默认输出到 stdout）",
    )
    parser.add_argument(
        "--output-zip",
        "-z",
        type=str,
        default=None,
        help="输出 zip 文件路径（推荐用于分享）",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="仅导出 plan.md",
    )
    parser.add_argument(
        "--regression-only",
        action="store_true",
        help="仅导出 regression.md",
    )

    args = parser.parse_args()

    # 验证参数
    if args.plan_only and args.regression_only:
        print("❌ 错误: --plan-only 和 --regression-only 不能同时使用", file=sys.stderr)
        return 1

    if args.output_dir and args.output_zip:
        print("❌ 错误: --output-dir 和 --output-zip 不能同时使用", file=sys.stderr)
        return 1

    if args.output_zip and (args.plan_only or args.regression_only):
        print(
            "❌ 错误: --output-zip 模式不支持 --plan-only 或 --regression-only",
            file=sys.stderr,
        )
        return 1

    try:
        # ZIP 模式
        if args.output_zip:
            output_zip = Path(args.output_zip)
            result = export_iteration_zip(args.iteration_number, output_zip=output_zip)

            # 输出警告
            if result.warnings:
                print(format_warnings(result.warnings), file=sys.stderr)

            print(f"✅ Iteration {args.iteration_number} 草稿已打包")
            print()
            print(f"📦 {result.zip_path}")
            print()
            print("包内容:")
            print("  📄 README.md    - 说明文件")
            if result.plan_content:
                print("  📄 plan.md      - 迭代计划草稿")
            if result.regression_content:
                print("  📄 regression.md - 回归记录草稿")
            print()
            print("⚠️  提醒: 导出内容来源于本地草稿，不是 SSOT。")
            print("    请勿在版本化文档中链接 .iteration/ 路径。")
            return 0

        # 目录模式
        output_dir = Path(args.output_dir) if args.output_dir else None
        result = export_iteration(args.iteration_number, output_dir=output_dir)

        # 输出警告
        if result.warnings:
            print(format_warnings(result.warnings), file=sys.stderr)

        if output_dir is not None:
            # 输出到文件模式
            print(f"✅ Iteration {args.iteration_number} 草稿导出完成")
            print()
            print("导出的文件:")
            for f in result.output_files:
                print(f"  📄 {f}")
            print()
            print("⚠️  提醒: 导出内容来源于本地草稿，不是 SSOT。")
            print("    请勿在版本化文档中链接 .iteration/ 路径。")
        else:
            # 输出到 stdout 模式
            if not args.regression_only and result.plan_content:
                print("=" * 80)
                print(f"# plan.md (Iteration {args.iteration_number})")
                print("=" * 80)
                print()
                print(result.plan_content)
                print()

            if not args.plan_only and result.regression_content:
                print("=" * 80)
                print(f"# regression.md (Iteration {args.iteration_number})")
                print("=" * 80)
                print()
                print(result.regression_content)
                print()

        return 0

    except SourceNotFoundError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("请确保本地迭代目录存在:", file=sys.stderr)
        print(f"  .iteration/{args.iteration_number}/", file=sys.stderr)
        print(file=sys.stderr)
        print("使用以下命令初始化本地迭代:", file=sys.stderr)
        print(
            f"   python scripts/iteration/init_local_iteration.py {args.iteration_number}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
