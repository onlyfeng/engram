# -*- coding: utf-8 -*-
"""
public_api 导入契约测试辅助模块

提供用于 public_api Tier A/B 分层导入测试的辅助工具：
- run_subprocess(): 在隔离子进程中执行 Python 脚本
- make_blocking_finder_code(): 生成 sys.meta_path BlockingFinder 代码
- get_regex_validation_code(): 生成 ImportError 消息结构正则校验代码

设计原则：
- 使用 subprocess 进行真正的进程隔离测试，避免 sys.modules 污染
- 使用 sys.meta_path BlockingFinder 模拟模块导入失败
- 支持验证 __cause__ 异常链正确保留

使用示例：
    from tests.gateway.helpers.public_api_import_contract_helpers import (
        run_subprocess,
        make_blocking_finder_code,
    )

    blocking_code = make_blocking_finder_code("engram.gateway.logbook_adapter")
    script = blocking_code + '''
    try:
        from engram.gateway.public_api import LogbookAdapter
    except ImportError as e:
        print("OK")
    '''
    result = run_subprocess(script)
    assert result.returncode == 0
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional


def get_pythonpath() -> str:
    """
    获取 PYTHONPATH，确保子进程可导入 src/ 目录

    Returns:
        src/ 目录的绝对路径字符串
    """
    repo_root = Path(__file__).parent.parent.parent.parent
    src_path = repo_root / "src"
    return str(src_path)


def run_subprocess(
    script: str, env_vars: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """
    在隔离子进程中执行 Python 脚本

    用于 public_api 导入契约测试，确保进程级隔离避免 sys.modules 污染。

    Args:
        script: 要执行的 Python 脚本内容
        env_vars: 额外的环境变量（可选）

    Returns:
        subprocess.CompletedProcess 结果对象

    使用示例：
        result = run_subprocess('''
        from engram.gateway.public_api import RequestContext
        print("OK")
        ''')
        assert result.returncode == 0
        assert "OK" in result.stdout
    """
    # 构建干净的环境变量（排除 PROJECT_KEY 和 POSTGRES_DSN）
    clean_env = {k: v for k, v in os.environ.items() if k not in ("PROJECT_KEY", "POSTGRES_DSN")}
    clean_env["PYTHONPATH"] = get_pythonpath()

    # 添加额外的环境变量
    if env_vars:
        clean_env.update(env_vars)

    return subprocess.run(
        [sys.executable, "-c", script],
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def make_blocking_finder_code(blocked_module: str) -> str:
    """
    生成 sys.meta_path BlockingFinder 代码

    生成的代码在子进程中执行时，会阻断指定模块的导入，
    触发 ImportError 以模拟依赖缺失场景。

    Args:
        blocked_module: 要阻断的完整模块名（如 engram.gateway.logbook_adapter）

    Returns:
        BlockingFinder Python 代码字符串

    使用示例：
        blocking_code = make_blocking_finder_code("engram.gateway.logbook_adapter")
        script = blocking_code + '''
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("应触发 ImportError")
        except ImportError:
            print("OK")
        '''
    """
    return f"""\
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec

class BlockingFinder(MetaPathFinder):
    '''阻断 {blocked_module} 模块导入'''
    BLOCKED_MODULES = frozenset([
        '{blocked_module}',
    ])

    def find_spec(self, fullname, path, target=None):
        if fullname in self.BLOCKED_MODULES:
            return ModuleSpec(fullname, BlockingLoader(fullname))
        return None

class BlockingLoader:
    def __init__(self, fullname):
        self.fullname = fullname

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise ImportError(
            f"[BlockingFinder] 模块 '{{self.fullname}}' 被阻断以模拟依赖缺失"
        )

sys.meta_path.insert(0, BlockingFinder())
"""


# 预生成的常用 BlockingFinder 代码
BLOCKING_LOGBOOK_ADAPTER_CODE = make_blocking_finder_code("engram.gateway.logbook_adapter")


def get_regex_validation_code() -> str:
    """
    生成 ImportError 消息结构正则校验的 Python 代码

    用于在子进程中验证 ImportError 消息是否符合
    docs/architecture/gateway_public_api_surface.md §Tier B 失败语义 定义的格式。

    Returns:
        包含 parse_error 函数定义的 Python 代码字符串

    使用示例：
        regex_code = get_regex_validation_code()
        script = blocking_code + regex_code + '''
        try:
            from engram.gateway.public_api import LogbookAdapter
        except ImportError as e:
            fields = parse_error(str(e))
            assert fields is not None
        '''
    """
    return '''\
import re

# 结构校验正则（宽松匹配：允许全角/半角冒号和括号）
_PATTERN = re.compile(
    r"无法导入\\s*'([^']+)'\\s*[（(]来自\\s*([^)）]+)[)）]"
    r".*?"
    r"原因[:：]\\s*(.+?)"
    r"\\n\\s*\\n"
    r"(.+)",
    re.DOTALL,
)

def parse_error(msg):
    """解析 ImportError 消息，返回四字段字典或 None"""
    match = _PATTERN.match(msg)
    if match:
        return {
            "symbol_name": match.group(1).strip(),
            "module_path": match.group(2).strip(),
            "original_error": match.group(3).strip(),
            "install_hint": match.group(4).strip(),
        }
    return None
'''


# ============================================================================
# Tier B 符号测试规格（用于 parametrized 测试矩阵）
# ============================================================================


class TierBSymbolSpec(NamedTuple):
    """Tier B 符号测试规格

    Attributes:
        symbol_name: 符号名（如 LogbookAdapter）
        module_path: 相对模块路径（如 .logbook_adapter）
        blocked_module: 需要阻断的完整模块名（如 engram.gateway.logbook_adapter）
    """

    symbol_name: str
    module_path: str
    blocked_module: str


# 与 public_api.py 中 _TIER_B_LAZY_IMPORTS 保持同步的测试矩阵
# 确保覆盖所有 Tier B 符号
TIER_B_SYMBOL_SPECS: list[TierBSymbolSpec] = [
    # logbook_adapter 模块
    TierBSymbolSpec("LogbookAdapter", ".logbook_adapter", "engram.gateway.logbook_adapter"),
    TierBSymbolSpec("get_adapter", ".logbook_adapter", "engram.gateway.logbook_adapter"),
    TierBSymbolSpec("get_reliability_report", ".logbook_adapter", "engram.gateway.logbook_adapter"),
    # tool_executor 模块
    TierBSymbolSpec(
        "execute_tool",
        ".entrypoints.tool_executor",
        "engram.gateway.entrypoints.tool_executor",
    ),
    # mcp_rpc 模块
    TierBSymbolSpec("dispatch_jsonrpc_request", ".mcp_rpc", "engram.gateway.mcp_rpc"),
    TierBSymbolSpec("JsonRpcDispatchResult", ".mcp_rpc", "engram.gateway.mcp_rpc"),
]


# ============================================================================
# ImportError 消息结构解析（用于本地契约验证）
# ============================================================================

import re


class ImportErrorFields(NamedTuple):
    """ImportError 错误消息解析结果

    四个必需字段（与 public_api._IMPORT_ERROR_TEMPLATE 保持一致）：
    - symbol_name: 导入失败的符号名（如 LogbookAdapter）
    - module_path: 来源模块路径（如 .logbook_adapter）
    - original_error: 原始错误信息
    - install_hint: 安装指引
    """

    symbol_name: str
    module_path: str
    original_error: str
    install_hint: str


# 结构校验正则（宽松匹配：允许全角/半角冒号和括号）
# 格式参考：docs/architecture/gateway_public_api_surface.md §Tier B 失败语义
_IMPORT_ERROR_STRUCT_PATTERN = re.compile(
    r"无法导入\s*'([^']+)'\s*[（(]来自\s*([^)）]+)[)）]"  # 第一行：symbol_name, module_path
    r".*?"  # 中间分隔（允许任意字符）
    r"原因[:：]\s*(.+?)"  # 原因字段（支持半角/全角冒号）
    r"\n\s*\n"  # 空行分隔
    r"(.+)",  # install_hint（剩余内容）
    re.DOTALL,
)


def parse_import_error_structure(error_msg: str) -> Optional[ImportErrorFields]:
    """
    解析 ImportError 消息结构，提取四个必需字段。

    仅校验结构存在性，不锁定具体空格/标点细节。

    Args:
        error_msg: ImportError 的字符串消息

    Returns:
        ImportErrorFields 如果结构匹配，否则 None
    """
    match = _IMPORT_ERROR_STRUCT_PATTERN.match(error_msg)
    if match:
        return ImportErrorFields(
            symbol_name=match.group(1).strip(),
            module_path=match.group(2).strip(),
            original_error=match.group(3).strip(),
            install_hint=match.group(4).strip(),
        )
    return None


__all__ = [
    # 子进程执行
    "run_subprocess",
    "get_pythonpath",
    # BlockingFinder 代码生成
    "make_blocking_finder_code",
    "BLOCKING_LOGBOOK_ADAPTER_CODE",
    # 正则校验代码生成
    "get_regex_validation_code",
    # Tier B 符号测试规格
    "TierBSymbolSpec",
    "TIER_B_SYMBOL_SPECS",
    # ImportError 消息解析
    "ImportErrorFields",
    "parse_import_error_structure",
]
