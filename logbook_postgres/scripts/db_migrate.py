#!/usr/bin/env python3
"""
兼容入口: db_migrate.py
"""

from engram.logbook.migrate import (
    run_migrate,
    run_all_checks,
    run_precheck,
    check_schemas_exist,
    check_tables_exist,
    check_columns_exist,
    check_indexes_exist,
    check_triggers_exist,
    check_matviews_exist,
)


def main() -> None:
    from engram.logbook.cli.db_migrate import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
