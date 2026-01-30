#!/usr/bin/env python3
"""
engram_logbook.cli.logbook - Logbook CLI 主入口

这是 `logbook` 命令行工具的入口点。

使用方式:
    logbook create_item --item-type task --title "My Task"
    logbook add_event --item-id 1 --event-type comment
    logbook health
    logbook validate
    logbook render_views

安装后可直接使用:
    pip install -e .
    logbook --help
"""

import sys


def main() -> int:
    """
    Logbook CLI 主入口点
    """
    import argparse
    from engram.logbook.db import get_connection
    from engram.logbook.errors import EngramError, make_error_result
    from engram.logbook.io import add_output_arguments, get_output_options, output_json
    from engram.logbook.migrate import run_all_checks

    parser = argparse.ArgumentParser(
        prog="engram-logbook",
        description="Engram Logbook 命令行工具",
    )
    parser.add_argument("--version", action="version", version="engram-logbook 0.1.0")
    add_output_arguments(parser)

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # health 子命令
    health_parser = subparsers.add_parser("health", help="检查数据库健康状态")
    health_parser.add_argument("--dsn", help="PostgreSQL 连接字符串")
    add_output_arguments(health_parser)

    # create_item 子命令（最小实现，保证输出格式）
    create_parser = subparsers.add_parser("create_item", help="创建 item")
    create_parser.add_argument("--item-type")
    create_parser.add_argument("--title")
    add_output_arguments(create_parser)

    # add_event 子命令
    event_parser = subparsers.add_parser("add_event", help="添加 event")
    event_parser.add_argument("--item-id")
    event_parser.add_argument("--event-type")
    add_output_arguments(event_parser)

    # attach 子命令
    attach_parser = subparsers.add_parser("attach", help="添加 attachment")
    attach_parser.add_argument("--item-id")
    attach_parser.add_argument("--kind")
    attach_parser.add_argument("--uri")
    attach_parser.add_argument("--sha256")
    attach_parser.add_argument("--size-bytes", type=int, default=None)
    add_output_arguments(attach_parser)

    # set_kv 子命令
    kv_parser = subparsers.add_parser("set_kv", help="设置 KV")
    kv_parser.add_argument("--namespace")
    kv_parser.add_argument("--key")
    kv_parser.add_argument("--value")
    add_output_arguments(kv_parser)

    # render_views 子命令
    render_parser = subparsers.add_parser("render_views", help="生成视图文件")
    render_parser.add_argument("--out-dir")
    render_parser.add_argument("--log-event", action="store_true")
    render_parser.add_argument("--item-id")
    add_output_arguments(render_parser)

    args = parser.parse_args()
    opts = get_output_options(args)

    if args.command is None:
        parser.print_help()
        return 0

    def output_invalid_args(message: str) -> int:
        output_json(
            make_error_result(code="INVALID_ARGS", message=message),
            pretty=opts["pretty"],
            quiet=opts["quiet"],
            json_out=opts["json_out"],
        )
        return 2

    try:
        if args.command == "health":
            dsn = args.dsn or None
            with get_connection(dsn=dsn) as conn:
                result = run_all_checks(conn)
            output_json(result, pretty=opts["pretty"], quiet=opts["quiet"], json_out=opts["json_out"])
            return 0 if result.get("ok") else 1

        if args.command == "create_item":
            if not args.item_type or not args.title:
                return output_invalid_args("缺少必需参数: --item-type, --title")
            output_json(
                {"ok": False, "code": "NOT_IMPLEMENTED", "message": "create_item 未实现"},
                pretty=opts["pretty"],
                quiet=opts["quiet"],
                json_out=opts["json_out"],
            )
            return 1

        if args.command == "add_event":
            if not args.item_id or not args.event_type:
                return output_invalid_args("缺少必需参数: --item-id, --event-type")
            output_json(
                {"ok": False, "code": "NOT_IMPLEMENTED", "message": "add_event 未实现"},
                pretty=opts["pretty"],
                quiet=opts["quiet"],
                json_out=opts["json_out"],
            )
            return 1

        if args.command == "attach":
            if not args.item_id or not args.kind or not args.uri or not args.sha256:
                return output_invalid_args("缺少必需参数: --item-id, --kind, --uri, --sha256")
            output_json(
                {"ok": False, "code": "NOT_IMPLEMENTED", "message": "attach 未实现"},
                pretty=opts["pretty"],
                quiet=opts["quiet"],
                json_out=opts["json_out"],
            )
            return 1

        if args.command == "set_kv":
            if not args.namespace or not args.key or args.value is None:
                return output_invalid_args("缺少必需参数: --namespace, --key, --value")
            output_json(
                {"ok": False, "code": "NOT_IMPLEMENTED", "message": "set_kv 未实现"},
                pretty=opts["pretty"],
                quiet=opts["quiet"],
                json_out=opts["json_out"],
            )
            return 1

        if args.command == "render_views":
            if args.log_event and not args.item_id:
                return output_invalid_args("使用 --log-event 时必须指定 --item-id")
            output_json(
                {"ok": False, "code": "NOT_IMPLEMENTED", "message": "render_views 未实现"},
                pretty=opts["pretty"],
                quiet=opts["quiet"],
                json_out=opts["json_out"],
            )
            return 1

        output_json(
            make_error_result(code="UNKNOWN_COMMAND", message=f"未知命令: {args.command}"),
            pretty=opts["pretty"],
            quiet=opts["quiet"],
            json_out=opts["json_out"],
        )
        return 2
    except EngramError as e:
        output_json(e.to_dict(), pretty=opts["pretty"], quiet=opts["quiet"], json_out=opts["json_out"])
        return e.exit_code
    except Exception as e:
        output_json(
            make_error_result(code="UNEXPECTED_ERROR", message=str(e)),
            pretty=opts["pretty"],
            quiet=opts["quiet"],
            json_out=opts["json_out"],
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
