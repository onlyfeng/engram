# -*- coding: utf-8 -*-
"""
SQL 迁移文件安全性检查

扫描 sql/**/*.sql 文件，检测高危 SQL 语句：
1. Denylist（直接 fail）: 绝对禁止的危险语句
2. Allowlist（需要标记）: 可接受但需要 `-- SAFE:` 或 `-- BREAKING:` 注释的语句

安全标记说明：
- `-- SAFE: <reason>` - 表示该语句已评估为安全，附带原因说明
- `-- BREAKING: <reason>` - 表示该语句是破坏性变更，需要在发布说明中记录

SSOT: sql/ 目录
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

# ---------- 常量定义 ----------

SQL_DIR = Path(__file__).parent.parent.parent / "sql"
VERIFY_DIR = SQL_DIR / "verify"

# 安全标记模式：必须在语句所在行或前一行包含
SAFE_MARKER_PATTERN = re.compile(r"--\s*SAFE:\s*\S", re.IGNORECASE)
BREAKING_MARKER_PATTERN = re.compile(r"--\s*BREAKING:\s*\S", re.IGNORECASE)


@dataclass
class Violation:
    """安全检查违规记录"""

    file: str
    line_num: int
    line_content: str
    rule: str
    severity: Literal["DENY", "NEEDS_MARKER"]
    suggestion: str


# ---------- 高危语句规则（Denylist - 直接 fail）----------

# 这些语句绝对禁止，无法通过标记豁免
DENYLIST_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # DROP TABLE 没有 IF EXISTS - 可能导致迁移失败
    (
        re.compile(r"\bDROP\s+TABLE\s+(?!IF\s+EXISTS\b)", re.IGNORECASE),
        "DROP TABLE 必须使用 IF EXISTS",
        "改为: DROP TABLE IF EXISTS <table_name>;",
    ),
    # DROP DATABASE - 绝对禁止
    (
        re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE),
        "禁止在迁移脚本中 DROP DATABASE",
        "数据库删除应由运维手动执行",
    ),
    # TRUNCATE - 高危数据删除（豁免：临时表以 _ 开头的可使用 SAFE 标记豁免）
    (
        re.compile(r"\bTRUNCATE\s+(TABLE\s+)?(?!_)\w+", re.IGNORECASE),
        "禁止在迁移脚本中使用 TRUNCATE",
        "如需清理数据，使用 DELETE 并添加 WHERE 条件和安全标记",
    ),
    # DROP SCHEMA 没有 IF EXISTS
    (
        re.compile(r"\bDROP\s+SCHEMA\s+(?!IF\s+EXISTS\b)", re.IGNORECASE),
        "DROP SCHEMA 必须使用 IF EXISTS",
        "改为: DROP SCHEMA IF EXISTS <schema_name>;",
    ),
    # DELETE FROM 没有 WHERE 子句（单行检测，跨行 DELETE 需人工审查）
    (
        re.compile(r"\bDELETE\s+FROM\s+\S+\s*;", re.IGNORECASE),
        "DELETE FROM 必须有 WHERE 子句",
        "添加 WHERE 条件限制删除范围，并添加 -- SAFE: 或 -- BREAKING: 标记",
    ),
    # ALTER TABLE DROP COLUMN 没有 IF EXISTS
    (
        re.compile(
            r"\bALTER\s+TABLE\s+\S+\s+DROP\s+COLUMN\s+(?!IF\s+EXISTS\b)",
            re.IGNORECASE,
        ),
        "ALTER TABLE DROP COLUMN 必须使用 IF EXISTS",
        "改为: ALTER TABLE <table> DROP COLUMN IF EXISTS <column>;",
    ),
    # DROP CONSTRAINT 没有 IF EXISTS
    (
        re.compile(
            r"\bALTER\s+TABLE\s+\S+\s+DROP\s+CONSTRAINT\s+(?!IF\s+EXISTS\b)",
            re.IGNORECASE,
        ),
        "ALTER TABLE DROP CONSTRAINT 必须使用 IF EXISTS",
        "改为: ALTER TABLE <table> DROP CONSTRAINT IF EXISTS <constraint>;",
    ),
]

# ---------- 需要标记的语句规则（Allowlist - 需要 SAFE/BREAKING 标记）----------

# 这些语句允许使用，但必须在同一行或前一行包含安全标记
ALLOWLIST_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # DROP TABLE IF EXISTS - 需要说明原因
    (
        re.compile(r"\bDROP\s+TABLE\s+IF\s+EXISTS\b", re.IGNORECASE),
        "DROP TABLE IF EXISTS 需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # DROP INDEX IF EXISTS - 需要说明原因
    (
        re.compile(r"\bDROP\s+INDEX\s+IF\s+EXISTS\b", re.IGNORECASE),
        "DROP INDEX IF EXISTS 需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # DROP TRIGGER IF EXISTS - 需要说明原因
    (
        re.compile(r"\bDROP\s+TRIGGER\s+IF\s+EXISTS\b", re.IGNORECASE),
        "DROP TRIGGER IF EXISTS 需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # DROP FUNCTION IF EXISTS - 需要说明原因
    (
        re.compile(r"\bDROP\s+FUNCTION\s+IF\s+EXISTS\b", re.IGNORECASE),
        "DROP FUNCTION IF EXISTS 需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # DROP VIEW / MATERIALIZED VIEW IF EXISTS - 需要说明原因
    (
        re.compile(r"\bDROP\s+(MATERIALIZED\s+)?VIEW\s+IF\s+EXISTS\b", re.IGNORECASE),
        "DROP VIEW IF EXISTS 需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # DROP SCHEMA IF EXISTS - 需要说明原因
    (
        re.compile(r"\bDROP\s+SCHEMA\s+IF\s+EXISTS\b", re.IGNORECASE),
        "DROP SCHEMA IF EXISTS 需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # ALTER TABLE DROP COLUMN IF EXISTS - 需要说明原因
    (
        re.compile(
            r"\bALTER\s+TABLE\s+\S+\s+DROP\s+COLUMN\s+IF\s+EXISTS\b",
            re.IGNORECASE,
        ),
        "ALTER TABLE DROP COLUMN IF EXISTS 需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # ALTER TABLE DROP CONSTRAINT IF EXISTS - 需要说明原因
    (
        re.compile(
            r"\bALTER\s+TABLE\s+\S+\s+DROP\s+CONSTRAINT\s+IF\s+EXISTS\b",
            re.IGNORECASE,
        ),
        "ALTER TABLE DROP CONSTRAINT IF EXISTS 需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # DELETE FROM with WHERE - 需要说明原因
    (
        re.compile(r"\bDELETE\s+FROM\s+\S+\s+WHERE\b", re.IGNORECASE),
        "DELETE FROM ... WHERE 需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # UPDATE without specific pattern (mass update) - 需要说明原因
    (
        re.compile(r"\bUPDATE\s+\S+\s+SET\s+.*\bWHERE\b", re.IGNORECASE),
        "UPDATE ... SET ... WHERE 需要安全标记（数据修改）",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
    # TRUNCATE 临时表（以 _ 开头）- 需要说明原因
    (
        re.compile(r"\bTRUNCATE\s+(TABLE\s+)?_\w+", re.IGNORECASE),
        "TRUNCATE 临时表需要安全标记",
        "在语句前添加: -- SAFE: <原因> 或 -- BREAKING: <原因>",
    ),
]

# ---------- 白名单排除模式 ----------

# 在 DO $$ 块内的语句通常是条件执行，可以豁免部分检查
# 但仍需要检查 DENYLIST 中的绝对禁止项
DO_BLOCK_PATTERN = re.compile(r"\bDO\s*\$\$", re.IGNORECASE)
DO_BLOCK_END_PATTERN = re.compile(r"\$\$\s*;", re.IGNORECASE)

# 注释行应跳过
COMMENT_LINE_PATTERN = re.compile(r"^\s*--")

# 在 PL/pgSQL 函数体内的语句需要特殊处理
FUNCTION_BODY_MARKERS = ["AS $$", "AS $func$", "AS $body$", "LANGUAGE plpgsql"]


# ---------- 辅助函数 ----------


def get_all_sql_files() -> list[Path]:
    """获取所有 SQL 文件（主目录 + verify 子目录）"""
    files = list(SQL_DIR.glob("*.sql"))
    if VERIFY_DIR.is_dir():
        files.extend(VERIFY_DIR.glob("*.sql"))
    return sorted(files)


def get_relative_path(file_path: Path) -> str:
    """获取相对于 SQL_DIR 的路径字符串"""
    try:
        return str(file_path.relative_to(SQL_DIR))
    except ValueError:
        return file_path.name


def has_safety_marker(lines: list[str], line_idx: int, lookback: int = 5) -> bool:
    """
    检查指定行或其前 N 行是否有安全标记

    支持一个标记覆盖连续的多个相关语句（如多个 DROP 语句）

    Args:
        lines: 文件所有行
        line_idx: 当前行索引（0-based）
        lookback: 向前检查的最大行数（默认5行）

    Returns:
        是否有 SAFE: 或 BREAKING: 标记
    """
    # 检查当前行
    current_line = lines[line_idx]
    if SAFE_MARKER_PATTERN.search(current_line) or BREAKING_MARKER_PATTERN.search(current_line):
        return True

    # 向前检查最多 lookback 行
    for i in range(1, min(lookback + 1, line_idx + 1)):
        prev_line = lines[line_idx - i]
        if SAFE_MARKER_PATTERN.search(prev_line) or BREAKING_MARKER_PATTERN.search(prev_line):
            return True
        # 如果遇到空行后又有非 DROP/DELETE/UPDATE 的代码，停止向前查找
        # （避免标记影响范围过大）
        if prev_line.strip() and not _is_related_dangerous_statement(prev_line):
            # 如果这一行不是危险语句，但也不是注释或空行，停止查找
            if not is_comment_line(prev_line):
                break

    return False


def _is_related_dangerous_statement(line: str) -> bool:
    """检查是否是相关的危险语句（DROP/DELETE/UPDATE/TRUNCATE）"""
    dangerous_keywords = ["DROP", "DELETE", "UPDATE", "TRUNCATE"]
    line_upper = line.upper()
    return any(kw in line_upper for kw in dangerous_keywords)


def is_in_do_block(lines: list[str], line_idx: int) -> bool:
    """
    检查当前行是否在 DO $$ ... $$; 块内

    Args:
        lines: 文件所有行
        line_idx: 当前行索引（0-based）

    Returns:
        是否在 DO 块内
    """
    do_block_depth = 0

    for i in range(line_idx + 1):
        line = lines[i]
        # 检测 DO $$ 开始
        if DO_BLOCK_PATTERN.search(line):
            do_block_depth += 1
        # 检测 $$; 结束
        if DO_BLOCK_END_PATTERN.search(line) and do_block_depth > 0:
            do_block_depth -= 1

    return do_block_depth > 0


def is_in_function_body(lines: list[str], line_idx: int) -> bool:
    """
    检查当前行是否在函数体内（CREATE FUNCTION ... AS $$ ... $$ LANGUAGE）

    Args:
        lines: 文件所有行
        line_idx: 当前行索引（0-based）

    Returns:
        是否在函数体内
    """
    # 简化检测：向上查找是否有 CREATE FUNCTION 且未闭合
    in_function = False
    function_depth = 0

    for i in range(line_idx + 1):
        line = lines[i].upper()
        if "CREATE" in line and "FUNCTION" in line:
            in_function = True
        if in_function and ("AS $$" in lines[i] or "AS $FUNC$" in lines[i].upper()):
            function_depth += 1
        if function_depth > 0 and ("$$;" in lines[i] or "$FUNC$;" in lines[i].upper()):
            function_depth -= 1
            if function_depth == 0:
                in_function = False

    return function_depth > 0


def is_comment_line(line: str) -> bool:
    """检查是否是注释行"""
    return bool(COMMENT_LINE_PATTERN.match(line))


def scan_file_for_violations(file_path: Path) -> list[Violation]:
    """
    扫描单个 SQL 文件查找安全违规

    Args:
        file_path: SQL 文件路径

    Returns:
        违规列表
    """
    violations: list[Violation] = []
    rel_path = get_relative_path(file_path)

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 尝试 latin-1 编码
        content = file_path.read_text(encoding="latin-1")

    lines = content.split("\n")

    for line_idx, line in enumerate(lines):
        # 跳过注释行
        if is_comment_line(line):
            continue

        # 跳过空行
        if not line.strip():
            continue

        line_num = line_idx + 1  # 转为 1-based

        # 1. 检查 DENYLIST（绝对禁止）
        for pattern, rule, suggestion in DENYLIST_PATTERNS:
            if pattern.search(line):
                # 即使在 DO 块内也不允许（除非有特殊豁免）
                # 但在函数体内可以豁免（函数逻辑需要）
                if is_in_function_body(lines, line_idx):
                    continue

                violations.append(
                    Violation(
                        file=rel_path,
                        line_num=line_num,
                        line_content=line.strip(),
                        rule=rule,
                        severity="DENY",
                        suggestion=suggestion,
                    )
                )

        # 2. 检查 ALLOWLIST（需要标记）
        for pattern, rule, suggestion in ALLOWLIST_PATTERNS:
            if pattern.search(line):
                # 在函数体内豁免（函数逻辑需要）
                if is_in_function_body(lines, line_idx):
                    continue

                # 在 DO 块内的条件执行豁免
                if is_in_do_block(lines, line_idx):
                    # DO 块内的 DROP/DELETE 等通常是幂等迁移逻辑，豁免
                    continue

                # 检查是否有安全标记
                if not has_safety_marker(lines, line_idx):
                    violations.append(
                        Violation(
                            file=rel_path,
                            line_num=line_num,
                            line_content=line.strip(),
                            rule=rule,
                            severity="NEEDS_MARKER",
                            suggestion=suggestion,
                        )
                    )

    return violations


def scan_all_sql_files() -> list[Violation]:
    """
    扫描所有 SQL 文件

    Returns:
        所有违规列表
    """
    all_violations: list[Violation] = []

    for sql_file in get_all_sql_files():
        violations = scan_file_for_violations(sql_file)
        all_violations.extend(violations)

    return all_violations


# ---------- 测试类 ----------


class TestSqlMigrationsSafety:
    """SQL 迁移安全性测试"""

    def test_no_denylist_violations(self):
        """验证没有绝对禁止的危险语句（Denylist）"""
        all_violations = scan_all_sql_files()
        deny_violations = [v for v in all_violations if v.severity == "DENY"]

        if deny_violations:
            messages = []
            for v in deny_violations:
                messages.append(
                    f"\n  {v.file}:{v.line_num}: {v.rule}\n"
                    f"    内容: {v.line_content[:80]}...\n"
                    f"    建议: {v.suggestion}"
                )

            pytest.fail(
                f"发现 {len(deny_violations)} 个绝对禁止的危险 SQL 语句:\n" + "".join(messages)
            )

    def test_allowlist_has_safety_markers(self):
        """验证需要标记的语句都有 SAFE/BREAKING 标记"""
        all_violations = scan_all_sql_files()
        marker_violations = [v for v in all_violations if v.severity == "NEEDS_MARKER"]

        if marker_violations:
            messages = []
            for v in marker_violations:
                messages.append(
                    f"\n  {v.file}:{v.line_num}: {v.rule}\n"
                    f"    内容: {v.line_content[:80]}...\n"
                    f"    建议: {v.suggestion}"
                )

            pytest.fail(
                f"发现 {len(marker_violations)} 个缺少安全标记的语句:\n"
                + "".join(messages)
                + "\n\n请在语句前添加 '-- SAFE: <原因>' 或 '-- BREAKING: <原因>' 标记。"
            )

    def test_scan_reports_summary(self):
        """输出扫描统计摘要（始终通过，用于 CI 日志）"""
        sql_files = get_all_sql_files()
        all_violations = scan_all_sql_files()

        deny_count = sum(1 for v in all_violations if v.severity == "DENY")
        marker_count = sum(1 for v in all_violations if v.severity == "NEEDS_MARKER")

        print("\n=== SQL 迁移安全性扫描摘要 ===")
        print(f"扫描文件数: {len(sql_files)}")
        print(f"DENY 违规数: {deny_count}")
        print(f"需要标记数: {marker_count}")
        print(f"总违规数: {len(all_violations)}")

        # 此测试始终通过，用于输出日志
        # 实际检查由上面两个测试执行


class TestSafetyMarkerSyntax:
    """安全标记语法测试"""

    def test_safe_marker_is_valid(self):
        """验证 SAFE 标记语法"""
        valid_markers = [
            "-- SAFE: 幂等重建视图",
            "-- SAFE: 仅添加新索引，无风险",
            "--SAFE: compact format",
            "-- safe: lowercase also works",
        ]

        for marker in valid_markers:
            assert SAFE_MARKER_PATTERN.search(marker), f"应识别有效标记: {marker}"

    def test_breaking_marker_is_valid(self):
        """验证 BREAKING 标记语法"""
        valid_markers = [
            "-- BREAKING: 删除废弃列，需更新应用代码",
            "-- BREAKING: 重命名表，影响所有引用",
            "--BREAKING: compact format",
            "-- breaking: lowercase",
        ]

        for marker in valid_markers:
            assert BREAKING_MARKER_PATTERN.search(marker), f"应识别有效标记: {marker}"

    def test_invalid_markers_rejected(self):
        """验证无效标记不被识别"""
        invalid_markers = [
            "-- SAFE",  # 缺少冒号后的内容
            "-- SAFE:",  # 缺少原因
            "-- SAFE:  ",  # 只有空格
            "SAFE: not a comment",  # 不是注释
        ]

        for marker in invalid_markers:
            assert not SAFE_MARKER_PATTERN.search(marker), f"不应识别无效标记: {marker}"


class TestContextAwareness:
    """上下文感知测试"""

    def test_detects_do_block(self):
        """验证 DO 块检测"""
        content = """
DO $$
BEGIN
    DROP TABLE IF EXISTS temp_table;
END $$;
"""
        lines = content.split("\n")

        # DROP TABLE 行应在 DO 块内
        drop_line_idx = next(i for i, line in enumerate(lines) if "DROP TABLE" in line)
        assert is_in_do_block(lines, drop_line_idx), "应检测到在 DO 块内"

    def test_detects_function_body(self):
        """验证函数体检测"""
        content = """
CREATE OR REPLACE FUNCTION my_func() RETURNS void AS $$
BEGIN
    DELETE FROM temp_table WHERE id = 1;
END;
$$ LANGUAGE plpgsql;
"""
        lines = content.split("\n")

        # DELETE 行应在函数体内
        delete_line_idx = next(i for i, line in enumerate(lines) if "DELETE FROM" in line)
        assert is_in_function_body(lines, delete_line_idx), "应检测到在函数体内"


class TestSpecificPatterns:
    """特定模式检测测试"""

    def test_drop_table_without_if_exists_denied(self):
        """验证 DROP TABLE 没有 IF EXISTS 被拒绝"""
        pattern = DENYLIST_PATTERNS[0][0]

        assert pattern.search("DROP TABLE my_table;")
        assert pattern.search("DROP TABLE schema.my_table;")
        assert not pattern.search("DROP TABLE IF EXISTS my_table;")

    def test_drop_database_denied(self):
        """验证 DROP DATABASE 被拒绝"""
        pattern = DENYLIST_PATTERNS[1][0]

        assert pattern.search("DROP DATABASE mydb;")
        assert pattern.search("DROP DATABASE IF EXISTS mydb;")

    def test_truncate_denied(self):
        """验证 TRUNCATE 被拒绝"""
        pattern = DENYLIST_PATTERNS[2][0]

        assert pattern.search("TRUNCATE my_table;")
        assert pattern.search("TRUNCATE TABLE my_table;")

    def test_delete_without_where_denied(self):
        """验证 DELETE 没有 WHERE 被拒绝"""
        pattern = DENYLIST_PATTERNS[4][0]

        assert pattern.search("DELETE FROM my_table;")
        assert not pattern.search("DELETE FROM my_table WHERE id = 1;")


# ---------- CLI 入口（可独立运行）----------


def main():
    """独立运行扫描（用于 CI 脚本）"""
    import sys

    print("=== SQL 迁移安全性检查 ===\n")

    sql_files = get_all_sql_files()
    print(f"扫描 {len(sql_files)} 个 SQL 文件...\n")

    all_violations = scan_all_sql_files()

    deny_violations = [v for v in all_violations if v.severity == "DENY"]
    marker_violations = [v for v in all_violations if v.severity == "NEEDS_MARKER"]

    exit_code = 0

    if deny_violations:
        print("=" * 60)
        print("DENY 违规（绝对禁止的危险语句）:")
        print("=" * 60)
        for v in deny_violations:
            print(f"\n[DENY] {v.file}:{v.line_num}")
            print(f"  规则: {v.rule}")
            print(f"  内容: {v.line_content[:80]}...")
            print(f"  建议: {v.suggestion}")
        exit_code = 1

    if marker_violations:
        print("\n" + "=" * 60)
        print("需要安全标记的语句:")
        print("=" * 60)
        for v in marker_violations:
            print(f"\n[NEEDS_MARKER] {v.file}:{v.line_num}")
            print(f"  规则: {v.rule}")
            print(f"  内容: {v.line_content[:80]}...")
            print(f"  建议: {v.suggestion}")
        exit_code = 1

    print("\n" + "=" * 60)
    print("扫描摘要:")
    print("=" * 60)
    print(f"  扫描文件: {len(sql_files)}")
    print(f"  DENY 违规: {len(deny_violations)}")
    print(f"  需要标记: {len(marker_violations)}")
    print(f"  总违规数: {len(all_violations)}")

    if exit_code == 0:
        print("\n✓ SQL 迁移安全性检查通过")
    else:
        print("\n✗ SQL 迁移安全性检查失败")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
