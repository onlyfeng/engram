#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_mcp_jsonrpc_error_contract.py - MCP JSON-RPC 错误码合约一致性检查

功能:
  校验 schemas/mcp_jsonrpc_error_v2.schema.json 中的 error_reason enum
  与 src/engram/gateway/error_codes.py:McpErrorReason 的公开常量集合是否一致。

SSOT（单一事实来源）策略:
  - Schema 为 error.data.reason 枚举值的权威来源
  - 代码实现（McpErrorReason）跟随 Schema 同步
  - 本脚本执行双向验证，确保二者一致

检查规则:
  1. Schema enum 中的所有 reason 码必须在 McpErrorReason 中存在（或在豁免清单中）
  2. McpErrorReason 中的所有公开常量必须在 Schema enum 中存在（或在豁免清单中）
  3. 二者差集为空时通过，否则失败

豁免清单说明:
  - SCHEMA_ONLY_EXEMPT: 仅存在于 Schema 中的豁免项（当前为空）
  - CODE_ONLY_EXEMPT: 仅存在于代码中的豁免项（当前为空）
  - 新增豁免需说明原因并更新 docs/contracts/mcp_jsonrpc_error_v2.md

使用方法:
  python scripts/ci/check_mcp_jsonrpc_error_contract.py           # 标准检查
  python scripts/ci/check_mcp_jsonrpc_error_contract.py --verbose # 详细输出
  python scripts/ci/check_mcp_jsonrpc_error_contract.py --json    # JSON 格式输出

退出码:
  0 - 检查通过（二者一致或差异在豁免清单中）
  1 - 检查失败（存在未豁免的差异）
  2 - 配置错误（文件不存在、格式错误等）

集成:
  - Makefile: check-mcp-error-contract
  - CI: lint job 或独立 job

参见:
  - docs/contracts/mcp_jsonrpc_error_v2.md §13.3（SSOT 定义）
  - docs/contracts/mcp_jsonrpc_error_v2_drift_matrix.md（漂移矩阵）
  - src/engram/gateway/error_codes.py
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Set

# ============================================================================
# 常量定义
# ============================================================================

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Schema 文件路径
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "mcp_jsonrpc_error_v2.schema.json"

# error_codes.py 模块路径
ERROR_CODES_MODULE = "engram.gateway.error_codes"

# Schema 中 error_reason enum 的 JSON 路径
SCHEMA_ENUM_PATH = ["definitions", "error_reason", "enum"]

# 豁免清单：仅存在于 Schema 中的合法项
# 这些 reason 码在 Schema 中定义但不在 McpErrorReason 类中
# 通常是因为它们属于其他错误码类（如 ToolResultErrorCode）
#
# 注意：当前豁免清单为空，因为：
# - DEPENDENCY_MISSING 仅属于业务层 ToolResultErrorCode，不在 Schema 中定义
# - Schema enum 与 McpErrorReason 公开常量保持完全一致
# 参见: docs/contracts/mcp_jsonrpc_error_v2.md §3.2
SCHEMA_ONLY_EXEMPT: Set[str] = set()

# 豁免清单：仅存在于代码中的合法项
# 这些 reason 码在 McpErrorReason 中定义但不在 Schema 中
# 保留用于未来扩展
CODE_ONLY_EXEMPT: Set[str] = set()


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class ContractCheckResult:
    """合约检查结果"""

    ok: bool = True
    schema_path: str = ""
    module_path: str = ""
    # Schema 中的 enum 值
    schema_reasons: Set[str] = field(default_factory=set)
    # 代码中的常量
    code_reasons: Set[str] = field(default_factory=set)
    # 差异
    schema_only: Set[str] = field(default_factory=set)  # 仅在 Schema 中
    code_only: Set[str] = field(default_factory=set)  # 仅在代码中
    # 豁免后的真实差异
    schema_only_unexempt: Set[str] = field(default_factory=set)
    code_only_unexempt: Set[str] = field(default_factory=set)
    # 错误信息
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema_path": self.schema_path,
            "module_path": self.module_path,
            "schema_reasons": sorted(self.schema_reasons),
            "code_reasons": sorted(self.code_reasons),
            "schema_only": sorted(self.schema_only),
            "code_only": sorted(self.code_only),
            "schema_only_exempt": sorted(SCHEMA_ONLY_EXEMPT),
            "code_only_exempt": sorted(CODE_ONLY_EXEMPT),
            "schema_only_unexempt": sorted(self.schema_only_unexempt),
            "code_only_unexempt": sorted(self.code_only_unexempt),
            "error": self.error,
        }


# ============================================================================
# 颜色输出
# ============================================================================


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"  # No Color

    @classmethod
    def disable(cls):
        """禁用颜色输出（用于非 TTY 环境）"""
        cls.RED = ""
        cls.GREEN = ""
        cls.YELLOW = ""
        cls.BLUE = ""
        cls.NC = ""


# 非 TTY 环境禁用颜色
if not sys.stdout.isatty():
    Colors.disable()


def log_info(msg: str):
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")


def log_success(msg: str):
    print(f"{Colors.GREEN}[PASS]{Colors.NC} {msg}")


def log_warn(msg: str):
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}")


def log_error(msg: str):
    print(f"{Colors.RED}[FAIL]{Colors.NC} {msg}")


# ============================================================================
# 核心逻辑
# ============================================================================


def load_schema_enum(schema_path: Path) -> tuple[bool, Set[str], Optional[str]]:
    """
    从 Schema 文件加载 error_reason enum

    Args:
        schema_path: Schema 文件路径

    Returns:
        (success, enum_values, error_message)
    """
    if not schema_path.exists():
        return False, set(), f"Schema 文件不存在: {schema_path}"

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        return False, set(), f"Schema JSON 解析失败: {e}"
    except Exception as e:
        return False, set(), f"读取 Schema 文件失败: {e}"

    # 按路径提取 enum
    current = schema
    for key in SCHEMA_ENUM_PATH:
        if not isinstance(current, dict) or key not in current:
            return (
                False,
                set(),
                f"Schema 路径不存在: {'.'.join(SCHEMA_ENUM_PATH)}",
            )
        current = current[key]

    if not isinstance(current, list):
        return False, set(), f"Schema enum 不是数组: {type(current)}"

    return True, set(current), None


def load_code_constants() -> tuple[bool, Set[str], Optional[str]]:
    """
    从 McpErrorReason 类加载公开常量

    Returns:
        (success, constant_values, error_message)
    """
    try:
        # 添加 src 到 sys.path 以便导入
        src_path = PROJECT_ROOT / "src"
        if str(src_path) not in sys.path:
            sys.path.insert(0, str(src_path))

        from engram.gateway.error_codes import McpErrorReason

        # 提取所有公开常量（大写且不以 _ 开头）
        constants = set()
        for name in dir(McpErrorReason):
            if name.startswith("_"):
                continue
            if not name.isupper():
                continue
            value = getattr(McpErrorReason, name)
            if isinstance(value, str):
                constants.add(value)

        return True, constants, None

    except ImportError as e:
        return False, set(), f"导入 {ERROR_CODES_MODULE} 失败: {e}"
    except Exception as e:
        return False, set(), f"提取 McpErrorReason 常量失败: {e}"


def check_contract(schema_path: Path = SCHEMA_PATH) -> ContractCheckResult:
    """
    执行合约一致性检查

    Args:
        schema_path: Schema 文件路径

    Returns:
        ContractCheckResult
    """
    result = ContractCheckResult(
        schema_path=str(schema_path),
        module_path=ERROR_CODES_MODULE,
    )

    # 1. 加载 Schema enum
    ok, schema_reasons, error = load_schema_enum(schema_path)
    if not ok:
        result.ok = False
        result.error = error
        return result
    result.schema_reasons = schema_reasons

    # 2. 加载代码常量
    ok, code_reasons, error = load_code_constants()
    if not ok:
        result.ok = False
        result.error = error
        return result
    result.code_reasons = code_reasons

    # 3. 计算差异
    result.schema_only = schema_reasons - code_reasons
    result.code_only = code_reasons - schema_reasons

    # 4. 应用豁免
    result.schema_only_unexempt = result.schema_only - SCHEMA_ONLY_EXEMPT
    result.code_only_unexempt = result.code_only - CODE_ONLY_EXEMPT

    # 5. 判断是否通过
    if result.schema_only_unexempt or result.code_only_unexempt:
        result.ok = False

    return result


def format_fix_steps(result: ContractCheckResult) -> list[str]:
    """
    生成修复步骤提示

    Args:
        result: 检查结果

    Returns:
        修复步骤列表
    """
    steps = []

    if result.schema_only_unexempt:
        steps.append("Schema 中存在但代码中缺失的 reason 码:")
        for reason in sorted(result.schema_only_unexempt):
            steps.append(f"  - {reason}")
        steps.append("")
        steps.append("修复方案:")
        steps.append("  A) 在 src/engram/gateway/error_codes.py 的 McpErrorReason 类中添加对应常量")
        steps.append("  B) 或者在本脚本的 SCHEMA_ONLY_EXEMPT 中添加豁免（需说明原因）")
        steps.append("")

    if result.code_only_unexempt:
        steps.append("代码中存在但 Schema 中缺失的 reason 码:")
        for reason in sorted(result.code_only_unexempt):
            steps.append(f"  - {reason}")
        steps.append("")
        steps.append("修复方案:")
        steps.append("  A) 在 schemas/mcp_jsonrpc_error_v2.schema.json 的 error_reason.enum 中添加")
        steps.append("  B) 或者在本脚本的 CODE_ONLY_EXEMPT 中添加豁免（需说明原因）")
        steps.append("")

    return steps


# ============================================================================
# 主入口
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="MCP JSON-RPC 错误码合约一致性检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细输出",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="JSON 格式输出",
    )

    parser.add_argument(
        "--schema-path",
        type=Path,
        default=SCHEMA_PATH,
        help=f"Schema 文件路径（默认 {SCHEMA_PATH}）",
    )

    args = parser.parse_args()

    # 执行检查
    result = check_contract(schema_path=args.schema_path)

    # JSON 输出
    if args.json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        sys.exit(0 if result.ok else 1)

    # 标准输出
    if args.verbose:
        log_info(f"Schema 路径: {result.schema_path}")
        log_info(f"模块路径: {result.module_path}")
        print("")

    if result.error:
        log_error(f"配置错误: {result.error}")
        sys.exit(2)

    if args.verbose:
        print(f"Schema enum ({len(result.schema_reasons)} 项):")
        for reason in sorted(result.schema_reasons):
            print(f"  - {reason}")
        print("")

        print(f"McpErrorReason ({len(result.code_reasons)} 项):")
        for reason in sorted(result.code_reasons):
            print(f"  - {reason}")
        print("")

        if result.schema_only:
            print(f"仅在 Schema 中 ({len(result.schema_only)} 项):")
            for reason in sorted(result.schema_only):
                exempt = " (豁免)" if reason in SCHEMA_ONLY_EXEMPT else ""
                print(f"  - {reason}{exempt}")
            print("")

        if result.code_only:
            print(f"仅在代码中 ({len(result.code_only)} 项):")
            for reason in sorted(result.code_only):
                exempt = " (豁免)" if reason in CODE_ONLY_EXEMPT else ""
                print(f"  - {reason}{exempt}")
            print("")

    # 输出结果
    if result.ok:
        log_success("MCP JSON-RPC 错误码合约一致性检查通过")
        if args.verbose:
            if result.schema_only or result.code_only:
                log_info(f"存在豁免差异: schema_only={len(result.schema_only)}, code_only={len(result.code_only)}")
    else:
        log_error("MCP JSON-RPC 错误码合约一致性检查失败")
        print("")
        fix_steps = format_fix_steps(result)
        for step in fix_steps:
            print(step)

    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
