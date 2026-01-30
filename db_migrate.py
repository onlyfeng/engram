#!/usr/bin/env python3
"""
db_migrate - 兼容导出（供测试导入）
"""

from __future__ import annotations

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
    is_testing_mode,
    validate_db_name,
    parse_db_name_from_dsn,
    replace_db_in_dsn,
    check_database_exists,
    ensure_database_exists,
    get_openmemory_schema,
    _build_lock_key,
    _acquire_advisory_lock,
    _release_advisory_lock,
)


def main() -> None:
    from engram.logbook.cli.db_migrate import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
