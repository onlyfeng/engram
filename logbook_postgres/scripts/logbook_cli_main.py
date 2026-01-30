#!/usr/bin/env python3
"""
兼容入口: logbook_cli_main.py
"""

from logbook_cli_main import app, artifacts_app, scm_app, make_ok_result, make_err_result


def main() -> None:
    app()


if __name__ == "__main__":
    main()
