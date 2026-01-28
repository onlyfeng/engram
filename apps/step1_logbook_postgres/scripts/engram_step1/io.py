"""
engram_step1.io - CLI I/O 工具模块

约定:
- 所有 CLI 输出默认为结构化 JSON（stdout）
- stdout: 机器可读的 JSON 输出
- stderr: 人读信息（日志、进度等）
- 成功: {ok: true, ...}
- 失败: {ok: false, code, message, detail}
- 错误时返回非 0 exit code
"""

import json
import sys
from datetime import datetime
from typing import Any, Callable, Dict, NoReturn, Optional

from .errors import EngramError, make_success_result, make_error_result


# =============================================================================
# JSON 输出
# =============================================================================

def output_json(data: Any, pretty: bool = False, quiet: bool = False) -> None:
    """
    输出 JSON 到 stdout

    Args:
        data: 要输出的数据
        pretty: 是否格式化输出
        quiet: 静默模式（仍然输出 JSON，但不输出 stderr 信息）
    """
    indent = 2 if pretty else None
    json_str = json.dumps(data, ensure_ascii=False, indent=indent, default=_json_serializer)
    print(json_str, file=sys.stdout)


def output_success(
    data: Optional[Dict[str, Any]] = None,
    pretty: bool = False,
    quiet: bool = False,
    **kwargs
) -> None:
    """
    输出成功结果到 stdout

    Args:
        data: 结果数据（合并到输出）
        pretty: 是否格式化输出
        quiet: 静默模式
        **kwargs: 额外字段
    """
    result = make_success_result(**kwargs)
    if data:
        result.update(data)
    output_json(result, pretty=pretty, quiet=quiet)


def output_error(
    error: EngramError,
    pretty: bool = False,
    quiet: bool = False,
    human_message: bool = True
) -> None:
    """
    输出错误到 stdout（JSON 格式）

    Args:
        error: EngramError 实例
        pretty: 是否格式化输出
        quiet: 静默模式（不输出人读信息到 stderr）
        human_message: 是否同时输出人读消息到 stderr
    """
    # JSON 输出到 stdout
    error_dict = error.to_dict()
    output_json(error_dict, pretty=pretty, quiet=quiet)

    # 人读信息到 stderr（除非 quiet）
    if human_message and not quiet:
        log_error(f"[{error.error_type}] {error.message}")


# =============================================================================
# stderr 人读信息
# =============================================================================

def log_info(message: str, quiet: bool = False) -> None:
    """
    输出信息到 stderr（人读）

    Args:
        message: 消息内容
        quiet: 静默模式（不输出）
    """
    if not quiet:
        print(message, file=sys.stderr)


def log_error(message: str, quiet: bool = False) -> None:
    """
    输出错误信息到 stderr（人读）

    Args:
        message: 错误消息
        quiet: 静默模式（不输出）
    """
    if not quiet:
        print(f"ERROR: {message}", file=sys.stderr)


def log_warning(message: str, quiet: bool = False) -> None:
    """
    输出警告信息到 stderr（人读）

    Args:
        message: 警告消息
        quiet: 静默模式（不输出）
    """
    if not quiet:
        print(f"WARN: {message}", file=sys.stderr)


def log_debug(message: str, verbose: bool = False) -> None:
    """
    输出调试信息到 stderr（人读）

    Args:
        message: 调试消息
        verbose: 详细模式才输出
    """
    if verbose:
        print(f"DEBUG: {message}", file=sys.stderr)


# =============================================================================
# 退出函数
# =============================================================================

def exit_with_error(
    error: EngramError,
    pretty: bool = False,
    quiet: bool = False
) -> NoReturn:
    """
    输出错误并以非 0 exit code 退出

    Args:
        error: EngramError 实例
        pretty: 是否格式化输出
        quiet: 静默模式
    """
    output_error(error, pretty=pretty, quiet=quiet)
    sys.exit(error.exit_code)


def exit_success(
    data: Optional[Dict[str, Any]] = None,
    pretty: bool = False,
    quiet: bool = False,
    **kwargs
) -> NoReturn:
    """
    输出成功结果并退出

    Args:
        data: 结果数据
        pretty: 是否格式化输出
        quiet: 静默模式
        **kwargs: 额外字段
    """
    output_success(data, pretty=pretty, quiet=quiet, **kwargs)
    sys.exit(0)


# =============================================================================
# JSON 序列化
# =============================================================================

def _json_serializer(obj: Any) -> Any:
    """
    JSON 序列化器，处理特殊类型

    Args:
        obj: 要序列化的对象

    Returns:
        可序列化的值
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


# =============================================================================
# CLI 装饰器
# =============================================================================

def cli_wrapper(func: Callable) -> Callable:
    """
    CLI 命令装饰器，统一处理异常和输出

    用法:
        @cli_wrapper
        def my_command(args):
            # 命令逻辑
            return {"result": "ok"}  # 返回值将作为成功结果输出
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        pretty = kwargs.pop("pretty", False)
        quiet = kwargs.pop("quiet", False)
        try:
            result = func(*args, **kwargs)
            if result is not None:
                output_success(result, pretty=pretty, quiet=quiet)
            else:
                output_success(pretty=pretty, quiet=quiet)
            sys.exit(0)
        except EngramError as e:
            exit_with_error(e, pretty=pretty, quiet=quiet)
        except KeyboardInterrupt:
            from .errors import EngramIOError

            exit_with_error(
                EngramIOError("操作被用户中断", {"signal": "SIGINT"}),
                pretty=pretty,
                quiet=quiet,
            )
        except Exception as e:
            from .errors import EngramError as BaseError

            exit_with_error(
                BaseError(f"未预期的错误: {e}", {"exception_type": type(e).__name__}),
                pretty=pretty,
                quiet=quiet,
            )

    return wrapper


# =============================================================================
# argparse 参数帮助函数
# =============================================================================

def add_output_arguments(parser, include_quiet: bool = True) -> None:
    """
    为 argparse.ArgumentParser 添加输出格式参数

    Args:
        parser: argparse.ArgumentParser 实例
        include_quiet: 是否包含 --quiet 参数
    """
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="格式化 JSON 输出（便于阅读）",
    )
    if include_quiet:
        parser.add_argument(
            "-q", "--quiet",
            action="store_true",
            help="静默模式（不输出 stderr 人读信息）",
        )


def get_output_options(args) -> Dict[str, bool]:
    """
    从 argparse.Namespace 提取输出选项

    Args:
        args: argparse.Namespace 实例

    Returns:
        {pretty: bool, quiet: bool}
    """
    return {
        "pretty": getattr(args, "pretty", False),
        "quiet": getattr(args, "quiet", False),
    }
