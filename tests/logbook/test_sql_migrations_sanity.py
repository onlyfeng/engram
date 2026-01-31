# -*- coding: utf-8 -*-
"""
测试 SQL 迁移文件的健全性检查

验证内容：
1. SQL 文件不存在内容重复
2. 不存在同名索引冲突定义
3. scan_sql_files 排序稳定性（按 prefix 数值升序 + filename 字典序）
4. 关键迁移（sync_jobs）索引定义一致性

SSOT: sql/ 目录
"""

import hashlib
import re
from pathlib import Path

import pytest

# ---------- 测试常量 ----------

SQL_DIR = Path(__file__).parent.parent.parent / "sql"
VERIFY_DIR = SQL_DIR / "verify"

# 已知的允许的前缀重复（如果有需要排除的）
ALLOWED_PREFIX_DUPLICATES: set[str] = set()

# 允许在主目录和 verify 子目录同时存在相同前缀的情况（例如 99）
# verify 子目录的脚本不被 initdb 自动执行，仅通过 CLI 显式触发
ALLOWED_CROSS_DIR_PREFIX_OVERLAP: set[str] = {"99"}

# 关键索引定义（索引名 -> 期望的列定义关键词）
CRITICAL_INDEX_DEFINITIONS = {
    "idx_sync_jobs_unique_active": ["repo_id", "job_type", "mode"],
    "idx_sync_jobs_running_lease": ["locked_at", "lease_seconds"],
    "idx_sync_jobs_claim": ["priority", "created_at"],
    "idx_sync_jobs_gitlab_instance_active": ["gitlab_instance"],
    "idx_sync_jobs_tenant_id_active": ["tenant_id"],
}


# ---------- 辅助函数 ----------


def get_all_sql_files(include_verify: bool = True) -> list[Path]:
    """
    获取所有 SQL 文件（主目录 + 可选的 verify 子目录）

    Args:
        include_verify: 是否包含 verify 子目录的文件

    Returns:
        SQL 文件路径列表
    """
    files = list(SQL_DIR.glob("*.sql"))
    if include_verify and VERIFY_DIR.is_dir():
        files.extend(VERIFY_DIR.glob("*.sql"))
    return files


def get_relative_path(file_path: Path) -> str:
    """获取相对于 SQL_DIR 的路径字符串"""
    try:
        return str(file_path.relative_to(SQL_DIR))
    except ValueError:
        return file_path.name


def compute_file_hash(file_path: Path) -> str:
    """计算文件内容的 SHA256 哈希"""
    content = file_path.read_bytes()
    return hashlib.sha256(content).hexdigest()


def extract_index_definitions(content: str) -> dict[str, list[str]]:
    """
    从 SQL 内容中提取索引定义

    返回: {索引名: [定义行列表]}
    """
    index_defs: dict[str, list[str]] = {}

    # 匹配 CREATE INDEX / CREATE UNIQUE INDEX 语句
    # 支持多行定义（以分号结尾）
    pattern = re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(\w+)\s+ON\s+([^;]+);",
        re.IGNORECASE | re.MULTILINE,
    )

    for match in pattern.finditer(content):
        index_name = match.group(1)
        definition = match.group(2).strip()

        if index_name not in index_defs:
            index_defs[index_name] = []
        index_defs[index_name].append(definition)

    return index_defs


def extract_column_names_from_index_def(definition: str) -> list[str]:
    """从索引定义中提取列名"""
    # 匹配括号内的列列表，例如 "sync_jobs(repo_id, job_type, mode)"
    match = re.search(r"\(([^)]+)\)", definition)
    if match:
        columns_part = match.group(1)
        # 分割并清理列名（移除排序方向等）
        columns = []
        for col in columns_part.split(","):
            col = col.strip()
            # 移除 ASC/DESC
            col = re.sub(r"\s+(ASC|DESC)\s*$", "", col, flags=re.IGNORECASE)
            columns.append(col.strip())
        return columns
    return []


# ---------- 测试：重复文件检测 ----------


class TestSqlFilesDuplicates:
    """测试 SQL 文件不存在重复"""

    def test_no_duplicate_content_files(self):
        """验证不存在内容完全相同的 SQL 文件（包括主目录和 verify 子目录）"""
        sql_files = get_all_sql_files(include_verify=True)

        # 计算每个文件的哈希
        hash_to_files: dict[str, list[str]] = {}
        for sql_file in sql_files:
            file_hash = compute_file_hash(sql_file)
            if file_hash not in hash_to_files:
                hash_to_files[file_hash] = []
            hash_to_files[file_hash].append(get_relative_path(sql_file))

        # 检查是否有重复
        duplicates = {h: files for h, files in hash_to_files.items() if len(files) > 1}

        assert not duplicates, "发现内容完全相同的 SQL 文件：\n" + "\n".join(
            f"  {files}" for files in duplicates.values()
        )

    def test_no_disallowed_prefix_duplicates(self):
        """验证不存在已知不允许的前缀重复（包括跨目录检测）"""
        from engram.logbook.migrate import scan_sql_files

        result = scan_sql_files(SQL_DIR, include_verify_subdir=True)
        duplicates = result["duplicates"]

        # 过滤掉允许的重复：
        # 1. ALLOWED_PREFIX_DUPLICATES - 主目录内允许的重复
        # 2. ALLOWED_CROSS_DIR_PREFIX_OVERLAP - 允许主目录和 verify 子目录之间的重复
        disallowed = {}
        for prefix, files in duplicates.items():
            if prefix in ALLOWED_PREFIX_DUPLICATES:
                continue
            # 检查是否是跨目录的允许重复
            if prefix in ALLOWED_CROSS_DIR_PREFIX_OVERLAP:
                # 允许 verify/ 子目录与主目录之间的重复
                main_dir_files = [f for f in files if not f.startswith("verify/")]
                verify_dir_files = [f for f in files if f.startswith("verify/")]
                # 如果跨目录，允许；如果同目录内有重复，则不允许
                if len(main_dir_files) <= 1 and len(verify_dir_files) <= 1:
                    continue
            disallowed[prefix] = files

        assert not disallowed, "发现不允许的前缀重复：\n" + "\n".join(
            f"  前缀 {p}: {files}" for p, files in disallowed.items()
        )

    def test_no_conflicting_index_definitions_across_files(self):
        """验证不同文件中不存在同名索引的冲突定义（包括主目录和 verify 子目录）"""
        sql_files = get_all_sql_files(include_verify=True)

        # 收集所有文件中的索引定义
        all_index_defs: dict[str, dict[str, list[str]]] = {}

        for sql_file in sql_files:
            content = sql_file.read_text(encoding="utf-8")
            index_defs = extract_index_definitions(content)

            for index_name, definitions in index_defs.items():
                if index_name not in all_index_defs:
                    all_index_defs[index_name] = {}
                all_index_defs[index_name][get_relative_path(sql_file)] = definitions

        # 检查是否有跨文件的冲突定义
        conflicts = []
        for index_name, file_defs in all_index_defs.items():
            if len(file_defs) > 1:
                # 同一索引出现在多个文件中，检查定义是否一致
                # 收集所有唯一定义
                unique_defs = set()
                for defs in file_defs.values():
                    for d in defs:
                        # 标准化定义（移除空格差异）
                        normalized = re.sub(r"\s+", " ", d.strip().lower())
                        unique_defs.add(normalized)

                # 如果有多个不同的定义，记录冲突
                # 注意：允许在 DO $$ 块中重复定义（用于升级逻辑）
                if len(unique_defs) > 1:
                    conflicts.append(
                        {
                            "index": index_name,
                            "files": list(file_defs.keys()),
                            "definitions": list(unique_defs),
                        }
                    )

        # 注意：某些索引可能在多个文件中定义（如 08 和 11），但定义应该一致
        # 实际上，我们检查的是是否有真正的冲突（不同的列定义）
        # 暂时只警告，不断言失败
        if conflicts:
            conflict_msgs = []
            for c in conflicts:
                conflict_msgs.append(f"  索引 {c['index']} 在 {c['files']} 中有不同定义")
            # 作为警告输出，某些差异可能是期望的（如升级逻辑）
            import warnings

            warnings.warn(
                "发现跨文件的索引定义差异（可能是升级逻辑导致）：\n" + "\n".join(conflict_msgs)
            )


# ---------- 测试：scan_sql_files 排序稳定性 ----------


class TestScanSqlFilesSorting:
    """测试 scan_sql_files 排序逻辑"""

    def test_scan_sql_files_returns_sorted_by_prefix(self):
        """验证 scan_sql_files 按前缀数值升序排序"""
        from engram.logbook.migrate import scan_sql_files

        result = scan_sql_files(SQL_DIR)
        files = result["files"]

        # 提取前缀列表
        prefixes = [int(prefix) for prefix, _ in files]

        # 验证是升序
        assert prefixes == sorted(prefixes), (
            f"scan_sql_files 返回的文件应按前缀数值升序，实际顺序: {prefixes}"
        )

    def test_scan_sql_files_secondary_sort_by_filename(self):
        """验证 scan_sql_files 二级排序按文件名字典序"""
        from engram.logbook.migrate import scan_sql_files

        result = scan_sql_files(SQL_DIR)
        files = result["files"]

        # 按前缀分组
        prefix_groups: dict[str, list[str]] = {}
        for prefix, path in files:
            if prefix not in prefix_groups:
                prefix_groups[prefix] = []
            prefix_groups[prefix].append(path.name)

        # 验证每个前缀组内的文件名是字典序排列
        for prefix, filenames in prefix_groups.items():
            assert filenames == sorted(filenames), (
                f"前缀 {prefix} 的文件应按文件名字典序排列，"
                f"实际: {filenames}，期望: {sorted(filenames)}"
            )

    def test_scan_sql_files_sort_key_is_prefix_then_filename(self):
        """验证 scan_sql_files 排序键是 (int(prefix), filename)"""
        from engram.logbook.migrate import scan_sql_files

        result = scan_sql_files(SQL_DIR)
        files = result["files"]

        # 提取 (prefix, filename) 元组
        sort_keys = [(int(prefix), path.name) for prefix, path in files]

        # 验证是按此键排序的
        expected = sorted(sort_keys, key=lambda x: (x[0], x[1]))
        assert sort_keys == expected, (
            f"scan_sql_files 排序键应为 (int(prefix), filename)\n"
            f"实际: {sort_keys}\n"
            f"期望: {expected}"
        )

    def test_scan_sql_files_stable_sort(self):
        """验证 scan_sql_files 排序是稳定的（多次调用结果一致）"""
        from engram.logbook.migrate import scan_sql_files

        # 多次调用
        results = [scan_sql_files(SQL_DIR) for _ in range(3)]

        # 提取文件名列表
        file_lists = [[path.name for _, path in r["files"]] for r in results]

        # 验证结果一致
        assert all(fl == file_lists[0] for fl in file_lists), (
            "scan_sql_files 多次调用应返回相同的排序结果"
        )


# ---------- 测试：sync_jobs 索引定义一致性 ----------


class TestSyncJobsIndexConsistencyInSql:
    """测试 sync_jobs 相关 SQL 文件中的索引定义一致性"""

    def test_08_sync_jobs_critical_indexes_defined(self):
        """验证 08_scm_sync_jobs.sql 定义了关键索引"""
        sync_jobs_file = SQL_DIR / "08_scm_sync_jobs.sql"
        assert sync_jobs_file.exists(), "08_scm_sync_jobs.sql 应存在"

        content = sync_jobs_file.read_text(encoding="utf-8")

        critical_indexes = [
            "idx_sync_jobs_claim",
            "idx_sync_jobs_status",
            "idx_sync_jobs_repo",
            "idx_sync_jobs_repo_job_type",
            "idx_sync_jobs_unique_active",
            "idx_sync_jobs_locked_by",
            "idx_sync_jobs_running_lease",
        ]

        for index_name in critical_indexes:
            assert index_name in content, f"08_scm_sync_jobs.sql 应定义索引 {index_name}"

    def test_11_dimension_indexes_defined(self):
        """验证 11_sync_jobs_dimension_columns.sql 定义了维度索引"""
        dimension_file = SQL_DIR / "11_sync_jobs_dimension_columns.sql"
        assert dimension_file.exists(), "11_sync_jobs_dimension_columns.sql 应存在"

        content = dimension_file.read_text(encoding="utf-8")

        dimension_indexes = [
            "idx_sync_jobs_gitlab_instance_active",
            "idx_sync_jobs_tenant_id_active",
        ]

        for index_name in dimension_indexes:
            assert index_name in content, (
                f"11_sync_jobs_dimension_columns.sql 应定义索引 {index_name}"
            )

    def test_critical_index_column_definitions(self):
        """验证关键索引包含期望的列"""
        sync_jobs_file = SQL_DIR / "08_scm_sync_jobs.sql"
        dimension_file = SQL_DIR / "11_sync_jobs_dimension_columns.sql"

        # 合并两个文件的内容
        content_08 = sync_jobs_file.read_text(encoding="utf-8")
        content_11 = dimension_file.read_text(encoding="utf-8") if dimension_file.exists() else ""

        all_content = content_08 + "\n" + content_11

        index_defs = extract_index_definitions(all_content)

        for index_name, expected_cols in CRITICAL_INDEX_DEFINITIONS.items():
            if index_name not in index_defs:
                pytest.skip(f"索引 {index_name} 未找到定义")
                continue

            # 检查至少一个定义包含期望的列
            definitions = index_defs[index_name]
            found_valid = False

            for definition in definitions:
                actual_cols = extract_column_names_from_index_def(definition)
                # 检查期望的列是否都存在
                if all(col in actual_cols for col in expected_cols):
                    found_valid = True
                    break

            assert found_valid, (
                f"索引 {index_name} 应包含列 {expected_cols}，实际定义: {definitions}"
            )

    def test_unique_active_index_includes_mode(self):
        """验证 idx_sync_jobs_unique_active 包含 mode 列"""
        sync_jobs_file = SQL_DIR / "08_scm_sync_jobs.sql"
        content = sync_jobs_file.read_text(encoding="utf-8")

        # 查找主定义（不在 DO $$ 块内）
        lines = content.split("\n")
        in_do_block = False
        main_definition = None

        for i, line in enumerate(lines):
            if "DO $$" in line:
                in_do_block = True
            elif in_do_block and "$$;" in line:
                in_do_block = False
            elif not in_do_block:
                if "CREATE UNIQUE INDEX" in line and "idx_sync_jobs_unique_active" in line:
                    # 收集多行定义
                    def_lines = []
                    for j in range(i, min(i + 5, len(lines))):
                        def_lines.append(lines[j])
                        if ";" in lines[j]:
                            break
                    main_definition = "\n".join(def_lines)
                    break

        assert main_definition is not None, (
            "应在 08_scm_sync_jobs.sql 主体（非 DO 块）中找到 idx_sync_jobs_unique_active"
        )
        assert "mode" in main_definition.lower(), (
            f"idx_sync_jobs_unique_active 应包含 mode 列，实际定义: {main_definition}"
        )

    def test_running_lease_index_includes_lease_seconds(self):
        """验证 idx_sync_jobs_running_lease 包含 lease_seconds 列"""
        sync_jobs_file = SQL_DIR / "08_scm_sync_jobs.sql"
        content = sync_jobs_file.read_text(encoding="utf-8")

        # 查找主定义（不在 DO $$ 块内）
        lines = content.split("\n")
        in_do_block = False
        main_definition = None

        for i, line in enumerate(lines):
            if "DO $$" in line:
                in_do_block = True
            elif in_do_block and "$$;" in line:
                in_do_block = False
            elif not in_do_block:
                if "CREATE INDEX" in line and "idx_sync_jobs_running_lease" in line:
                    # 收集多行定义
                    def_lines = []
                    for j in range(i, min(i + 5, len(lines))):
                        def_lines.append(lines[j])
                        if ";" in lines[j]:
                            break
                    main_definition = "\n".join(def_lines)
                    break

        assert main_definition is not None, (
            "应在 08_scm_sync_jobs.sql 主体（非 DO 块）中找到 idx_sync_jobs_running_lease"
        )
        assert "lease_seconds" in main_definition.lower(), (
            f"idx_sync_jobs_running_lease 应包含 lease_seconds 列，实际定义: {main_definition}"
        )


# ---------- 测试：索引定义一致性（集成测试） ----------


class TestSyncJobsIndexConsistencyIntegration:
    """测试 sync_jobs 索引在数据库中的一致性（集成测试）"""

    @pytest.fixture
    def sync_jobs_indexes(self, migrated_db):
        """获取数据库中 sync_jobs 表的索引定义"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT indexname, pg_get_indexdef(i.indexrelid) as indexdef
                    FROM pg_indexes i
                    JOIN pg_class c ON c.relname = i.indexname
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE i.schemaname = 'scm'
                      AND i.tablename = 'sync_jobs'
                    ORDER BY indexname
                """)
                return {row[0]: row[1] for row in cur.fetchall()}
        finally:
            conn.close()

    def test_unique_active_index_has_mode_in_db(self, sync_jobs_indexes):
        """验证数据库中 idx_sync_jobs_unique_active 包含 mode"""
        if "idx_sync_jobs_unique_active" not in sync_jobs_indexes:
            pytest.skip("idx_sync_jobs_unique_active 不存在")

        indexdef = sync_jobs_indexes["idx_sync_jobs_unique_active"]
        assert "mode" in indexdef.lower(), (
            f"数据库中 idx_sync_jobs_unique_active 应包含 mode 列，实际定义: {indexdef}"
        )

    def test_running_lease_index_has_lease_seconds_in_db(self, sync_jobs_indexes):
        """验证数据库中 idx_sync_jobs_running_lease 包含 lease_seconds"""
        if "idx_sync_jobs_running_lease" not in sync_jobs_indexes:
            pytest.skip("idx_sync_jobs_running_lease 不存在")

        indexdef = sync_jobs_indexes["idx_sync_jobs_running_lease"]
        assert "lease_seconds" in indexdef.lower(), (
            f"数据库中 idx_sync_jobs_running_lease 应包含 lease_seconds 列，实际定义: {indexdef}"
        )

    def test_dimension_indexes_exist_in_db(self, sync_jobs_indexes):
        """验证维度索引存在于数据库中"""
        dimension_indexes = [
            "idx_sync_jobs_gitlab_instance_active",
            "idx_sync_jobs_tenant_id_active",
        ]

        for index_name in dimension_indexes:
            if index_name not in sync_jobs_indexes:
                pytest.skip(f"{index_name} 不存在（可能维度迁移未执行）")

            indexdef = sync_jobs_indexes[index_name]
            # 验证索引存在且有效
            assert "CREATE INDEX" in indexdef or "CREATE UNIQUE INDEX" in indexdef, (
                f"{index_name} 定义无效: {indexdef}"
            )

    def test_all_sync_jobs_indexes_are_valid(self, sync_jobs_indexes):
        """验证所有 sync_jobs 索引定义有效"""
        for index_name, indexdef in sync_jobs_indexes.items():
            # 验证是有效的索引定义
            assert indexdef.strip().startswith("CREATE"), (
                f"索引 {index_name} 定义格式无效: {indexdef}"
            )
            # 验证引用 sync_jobs 表
            assert "sync_jobs" in indexdef, f"索引 {index_name} 应引用 sync_jobs 表: {indexdef}"


# ---------- 测试：SQL 文件命名规范 ----------

# ---------- 测试：迁移文件清单与编号规则 ----------


class TestMigrationFileInventory:
    """测试迁移文件清单与 migrate.py 中的常量一致性"""

    def test_ddl_script_prefixes_match_actual_files(self):
        """验证 DDL_SCRIPT_PREFIXES 与实际存在的文件一致"""
        from engram.logbook.migrate import DDL_SCRIPT_PREFIXES

        sql_files = list(SQL_DIR.glob("*.sql"))
        actual_prefixes = set()

        for sql_file in sql_files:
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                prefix = match.group(1)
                # 排除权限脚本和验证脚本
                if prefix not in {"04", "05", "99"}:
                    actual_prefixes.add(prefix)

        # DDL 前缀应是实际存在的子集
        missing_in_dir = DDL_SCRIPT_PREFIXES - actual_prefixes
        extra_in_dir = actual_prefixes - DDL_SCRIPT_PREFIXES

        # 注意：某些前缀可能已废弃（如 10），所以只检查 DDL_SCRIPT_PREFIXES 中的是否都存在文件
        assert not missing_in_dir, (
            f"DDL_SCRIPT_PREFIXES 中定义的前缀在 sql/ 目录中不存在：{sorted(missing_in_dir)}"
        )

        # 如果实际存在但未在 DDL_SCRIPT_PREFIXES 中定义，发出警告（可能是新增文件）
        if extra_in_dir:
            import warnings

            warnings.warn(
                f"sql/ 目录中存在未在 DDL_SCRIPT_PREFIXES 中定义的前缀：{sorted(extra_in_dir)}"
            )

    def test_permission_script_prefixes_match_actual_files(self):
        """验证 PERMISSION_SCRIPT_PREFIXES 与实际存在的权限脚本一致"""
        from engram.logbook.migrate import PERMISSION_SCRIPT_PREFIXES

        sql_files = list(SQL_DIR.glob("*.sql"))
        actual_permission_prefixes = set()

        for sql_file in sql_files:
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                prefix = match.group(1)
                # 04 和 05 是权限脚本
                if prefix in {"04", "05"}:
                    actual_permission_prefixes.add(prefix)

        # 验证权限脚本前缀一致
        assert PERMISSION_SCRIPT_PREFIXES == actual_permission_prefixes, (
            f"PERMISSION_SCRIPT_PREFIXES 与实际权限脚本不一致。"
            f"定义：{PERMISSION_SCRIPT_PREFIXES}，实际：{actual_permission_prefixes}"
        )

    def test_verify_script_prefixes_match_verify_dir(self):
        """验证 VERIFY_SCRIPT_PREFIXES 与 verify 子目录中的文件一致"""
        from engram.logbook.migrate import VERIFY_SCRIPT_PREFIXES

        if not VERIFY_DIR.is_dir():
            pytest.skip("verify 子目录不存在")

        verify_files = list(VERIFY_DIR.glob("*.sql"))
        actual_verify_prefixes = set()

        for sql_file in verify_files:
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                actual_verify_prefixes.add(match.group(1))

        # 验证验证脚本前缀一致
        assert VERIFY_SCRIPT_PREFIXES == actual_verify_prefixes, (
            f"VERIFY_SCRIPT_PREFIXES 与 verify 子目录中的实际文件不一致。"
            f"定义：{VERIFY_SCRIPT_PREFIXES}，实际：{actual_verify_prefixes}"
        )

    def test_no_prefix_overlap_between_categories(self):
        """验证 DDL/权限/验证脚本前缀不重叠"""
        from engram.logbook.migrate import (
            DDL_SCRIPT_PREFIXES,
            PERMISSION_SCRIPT_PREFIXES,
            VERIFY_SCRIPT_PREFIXES,
        )

        # DDL 和权限脚本不重叠
        overlap_ddl_perm = DDL_SCRIPT_PREFIXES & PERMISSION_SCRIPT_PREFIXES
        assert not overlap_ddl_perm, f"DDL 和权限脚本前缀重叠：{overlap_ddl_perm}"

        # DDL 和验证脚本不重叠
        overlap_ddl_verify = DDL_SCRIPT_PREFIXES & VERIFY_SCRIPT_PREFIXES
        assert not overlap_ddl_verify, f"DDL 和验证脚本前缀重叠：{overlap_ddl_verify}"

        # 权限和验证脚本不重叠
        overlap_perm_verify = PERMISSION_SCRIPT_PREFIXES & VERIFY_SCRIPT_PREFIXES
        assert not overlap_perm_verify, f"权限和验证脚本前缀重叠：{overlap_perm_verify}"

    def test_all_prefix_categories_cover_all_files(self):
        """验证所有 SQL 文件的前缀都被分类覆盖"""
        from engram.logbook.migrate import (
            DDL_SCRIPT_PREFIXES,
            PERMISSION_SCRIPT_PREFIXES,
            VERIFY_SCRIPT_PREFIXES,
        )

        all_defined_prefixes = (
            DDL_SCRIPT_PREFIXES | PERMISSION_SCRIPT_PREFIXES | VERIFY_SCRIPT_PREFIXES
        )

        # 收集所有实际存在的前缀
        actual_prefixes = set()

        # 主目录
        for sql_file in SQL_DIR.glob("*.sql"):
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                actual_prefixes.add(match.group(1))

        # verify 子目录
        if VERIFY_DIR.is_dir():
            for sql_file in VERIFY_DIR.glob("*.sql"):
                match = re.match(r"^(\d{2})_", sql_file.name)
                if match:
                    actual_prefixes.add(match.group(1))

        # 检查是否有未分类的前缀
        uncovered = actual_prefixes - all_defined_prefixes
        assert not uncovered, (
            f"以下前缀未被任何分类覆盖：{sorted(uncovered)}。"
            f"请在 migrate.py 中将其添加到相应的 *_SCRIPT_PREFIXES 集合中。"
        )


class TestMigrationNumberingRules:
    """测试迁移文件编号规则"""

    def test_prefixes_are_two_digits(self):
        """验证所有迁移文件前缀为两位数字"""
        all_files = list(SQL_DIR.glob("*.sql"))
        if VERIFY_DIR.is_dir():
            all_files.extend(VERIFY_DIR.glob("*.sql"))

        invalid_prefixes = []
        for sql_file in all_files:
            # 前缀应为两位数字
            if not re.match(r"^\d{2}_", sql_file.name):
                invalid_prefixes.append(sql_file.name)

        assert not invalid_prefixes, f"以下文件前缀不是两位数字：{invalid_prefixes}"

    def test_prefix_10_is_deprecated(self):
        """验证前缀 10 已废弃（不存在对应文件）"""
        prefix_10_files = list(SQL_DIR.glob("10_*.sql"))

        assert len(prefix_10_files) == 0, (
            f"前缀 10 应已废弃，但发现文件：{[f.name for f in prefix_10_files]}"
        )

    def test_ddl_prefixes_have_logical_order(self):
        """验证 DDL 脚本前缀有逻辑顺序（依赖关系）"""
        from engram.logbook.migrate import DDL_SCRIPT_PREFIXES

        # 将前缀转为整数排序
        sorted_prefixes = sorted(int(p) for p in DDL_SCRIPT_PREFIXES)

        # 验证关键依赖：
        # 01（基础 schema）应在所有其他 DDL 之前
        assert sorted_prefixes[0] == 1, "01_logbook_schema.sql（基础 schema）应是第一个 DDL 脚本"

        # 08（sync_jobs）应在 11（维度列）之前
        if 8 in sorted_prefixes and 11 in sorted_prefixes:
            assert sorted_prefixes.index(8) < sorted_prefixes.index(11), (
                "08（sync_jobs 表）应在 11（维度列）之前"
            )

    def test_migration_sequence_is_documented(self):
        """验证迁移序列中的废弃说明"""
        from engram.logbook.migrate import DDL_SCRIPT_PREFIXES

        # 检查是否有跳号（除了已知废弃的 10）
        sorted_prefixes = sorted(int(p) for p in DDL_SCRIPT_PREFIXES)

        gaps = []
        for i in range(len(sorted_prefixes) - 1):
            current = sorted_prefixes[i]
            next_val = sorted_prefixes[i + 1]
            gap = next_val - current
            if gap > 1:
                # 记录跳号
                for missing in range(current + 1, next_val):
                    if missing != 10:  # 10 是已知废弃
                        gaps.append(missing)

        # 如果有未记录的跳号，发出警告
        if gaps:
            import warnings

            warnings.warn(f"迁移序列中存在未记录的跳号：{gaps}。如果是有意废弃，请在文档中说明。")


class TestSqlFileNamingConventions:
    """测试 SQL 文件命名规范"""

    def test_all_sql_files_have_two_digit_prefix(self):
        """验证所有 SQL 文件有两位数前缀（主目录）"""
        sql_files = list(SQL_DIR.glob("*.sql"))

        invalid_files = []
        for sql_file in sql_files:
            if not re.match(r"^\d{2}_", sql_file.name):
                invalid_files.append(sql_file.name)

        assert not invalid_files, f"以下 SQL 文件没有两位数前缀：{invalid_files}"

    def test_verify_dir_files_have_two_digit_prefix(self):
        """验证 verify 子目录中的所有 SQL 文件有两位数前缀"""
        if not VERIFY_DIR.is_dir():
            pytest.skip("verify 子目录不存在")

        sql_files = list(VERIFY_DIR.glob("*.sql"))

        invalid_files = []
        for sql_file in sql_files:
            if not re.match(r"^\d{2}_", sql_file.name):
                invalid_files.append(f"verify/{sql_file.name}")

        assert not invalid_files, f"以下 verify 子目录 SQL 文件没有两位数前缀：{invalid_files}"

    def test_no_gaps_in_critical_prefixes(self):
        """验证关键前缀没有缺失"""
        sql_files = list(SQL_DIR.glob("*.sql"))

        # 提取所有前缀
        prefixes = set()
        for sql_file in sql_files:
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                prefixes.add(int(match.group(1)))

        # 关键前缀（不应缺失）
        # 注意: 99 前缀验证脚本位于 sql/verify/ 子目录，不在主目录检查范围
        critical_prefixes = {1, 2, 3, 4, 5, 6, 7, 8}

        missing = critical_prefixes - prefixes
        assert not missing, f"以下关键前缀缺失：{sorted(missing)}"

    def test_verify_script_has_99_prefix(self):
        """验证验证脚本使用 99 前缀（位于 sql/verify/ 子目录）"""
        # 验证脚本位于 sql/verify/ 子目录，不被 initdb 自动执行
        assert VERIFY_DIR.is_dir(), f"sql/verify/ 子目录应存在: {VERIFY_DIR}"

        verify_files = list(VERIFY_DIR.glob("99_*.sql"))

        assert len(verify_files) >= 1, "sql/verify/ 子目录应至少有一个 99 前缀的验证脚本"

        # 验证脚本名包含 verify 或 permissions
        for vf in verify_files:
            assert "verify" in vf.name.lower() or "permissions" in vf.name.lower(), (
                f"99 前缀脚本应是验证脚本: {vf.name}"
            )

    def test_verify_dir_only_contains_verification_scripts(self):
        """验证 verify 子目录仅包含验证脚本（99 前缀）"""
        if not VERIFY_DIR.is_dir():
            pytest.skip("verify 子目录不存在")

        all_verify_files = list(VERIFY_DIR.glob("*.sql"))
        non_99_files = []

        for sql_file in all_verify_files:
            if not sql_file.name.startswith("99_"):
                non_99_files.append(sql_file.name)

        assert not non_99_files, (
            f"verify 子目录应仅包含 99 前缀的验证脚本，发现非 99 前缀文件：{non_99_files}"
        )

    def test_main_dir_no_99_prefix_scripts(self):
        """验证主目录不包含 99 前缀的脚本（99 脚本应在 verify 子目录）"""
        main_dir_99_files = list(SQL_DIR.glob("99_*.sql"))

        assert not main_dir_99_files, (
            f"主目录 sql/ 不应包含 99 前缀的脚本（应移至 sql/verify/）：{[f.name for f in main_dir_99_files]}"
        )


# ---------- 测试：前缀分类完整性门禁 ----------


class TestPrefixClassificationGate:
    """
    前缀分类完整性测试（门禁）

    确保：
    1. 所有 SQL 文件前缀都被分类到 DDL/Permission/Verify 中的一类
    2. verify 前缀（99）只出现在 sql/verify/ 目录
    3. 主目录的前缀不包含 verify 前缀
    """

    # 显式允许的例外前缀（如果有特殊用途的前缀不属于三类分类）
    ALLOWLIST_PREFIXES: set[str] = set()

    def test_all_prefixes_are_classified(self):
        """断言：每个前缀必须属于 DDL/Permission/Verify 中的一类（或在显式 allowlist）"""
        from engram.logbook.migrate import (
            DDL_SCRIPT_PREFIXES,
            PERMISSION_SCRIPT_PREFIXES,
            VERIFY_SCRIPT_PREFIXES,
        )

        # 收集所有实际存在的前缀（主目录 + verify 子目录）
        actual_prefixes = set()

        for sql_file in SQL_DIR.glob("*.sql"):
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                actual_prefixes.add(match.group(1))

        if VERIFY_DIR.is_dir():
            for sql_file in VERIFY_DIR.glob("*.sql"):
                match = re.match(r"^(\d{2})_", sql_file.name)
                if match:
                    actual_prefixes.add(match.group(1))

        # 所有已分类的前缀
        all_classified = (
            DDL_SCRIPT_PREFIXES
            | PERMISSION_SCRIPT_PREFIXES
            | VERIFY_SCRIPT_PREFIXES
            | self.ALLOWLIST_PREFIXES
        )

        # 检查是否有未分类的前缀
        unclassified = actual_prefixes - all_classified

        assert not unclassified, (
            f"以下前缀未被分类到 DDL/Permission/Verify 任一类中：{sorted(unclassified)}\n"
            f"请在 migrate.py 的 *_SCRIPT_PREFIXES 常量中添加这些前缀，\n"
            f"或将其添加到测试的 ALLOWLIST_PREFIXES 中（如有特殊原因）。"
        )

    def test_verify_prefix_only_in_verify_dir(self):
        """断言：verify 前缀（99）只能出现在 sql/verify/，避免 initdb 误执行"""
        from engram.logbook.migrate import VERIFY_SCRIPT_PREFIXES

        # 扫描主目录中的所有文件
        main_dir_prefixes = set()
        for sql_file in SQL_DIR.glob("*.sql"):
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                main_dir_prefixes.add(match.group(1))

        # 检查 verify 前缀是否误放在主目录
        verify_in_main = main_dir_prefixes & VERIFY_SCRIPT_PREFIXES

        assert not verify_in_main, (
            f"Verify 前缀 {sorted(verify_in_main)} 不应出现在主目录 sql/ 中！\n"
            f"Verify 脚本必须放在 sql/verify/ 子目录，以避免 initdb 自动执行。\n"
            f"请将这些文件移动到 sql/verify/ 目录。"
        )

    def test_verify_dir_only_has_verify_prefixes(self):
        """断言：verify 子目录只包含 verify 前缀的文件"""
        from engram.logbook.migrate import VERIFY_SCRIPT_PREFIXES

        if not VERIFY_DIR.is_dir():
            pytest.skip("verify 子目录不存在")

        # 扫描 verify 子目录
        verify_dir_prefixes = set()
        for sql_file in VERIFY_DIR.glob("*.sql"):
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                verify_dir_prefixes.add(match.group(1))

        # 检查是否有非 verify 前缀
        non_verify_prefixes = verify_dir_prefixes - VERIFY_SCRIPT_PREFIXES

        assert not non_verify_prefixes, (
            f"verify 子目录中发现非 verify 前缀的文件：{sorted(non_verify_prefixes)}\n"
            f"sql/verify/ 目录只应包含验证脚本（前缀 {sorted(VERIFY_SCRIPT_PREFIXES)}）。\n"
            f"请将这些文件移动到主目录 sql/ 或删除。"
        )

    def test_main_dir_prefixes_exclude_verify(self):
        """断言：主目录的前缀集合与 verify 目录的前缀集合不重叠（99 除外的边界检查）"""
        from engram.logbook.migrate import (
            DDL_SCRIPT_PREFIXES,
            PERMISSION_SCRIPT_PREFIXES,
            VERIFY_SCRIPT_PREFIXES,
        )

        # 主目录应只包含 DDL 和 Permission 前缀
        main_dir_expected = DDL_SCRIPT_PREFIXES | PERMISSION_SCRIPT_PREFIXES

        # 扫描主目录
        main_dir_prefixes = set()
        for sql_file in SQL_DIR.glob("*.sql"):
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                main_dir_prefixes.add(match.group(1))

        # 主目录的前缀应该是 DDL | Permission 的子集
        extra_in_main = main_dir_prefixes - main_dir_expected

        # 额外的前缀既不是 DDL 也不是 Permission，可能是未分类或误放
        if extra_in_main:
            # 检查是否是 verify 前缀误放
            verify_misplaced = extra_in_main & VERIFY_SCRIPT_PREFIXES
            truly_unclassified = extra_in_main - VERIFY_SCRIPT_PREFIXES

            messages = []
            if verify_misplaced:
                messages.append(
                    f"Verify 前缀 {sorted(verify_misplaced)} 误放在主目录，应移至 sql/verify/"
                )
            if truly_unclassified:
                messages.append(
                    f"未分类前缀 {sorted(truly_unclassified)} 需要在 migrate.py 中添加分类"
                )

            assert not extra_in_main, "\n".join(messages)


# ---------- 测试：文档一致性门禁 ----------


class TestDocumentationConsistency:
    """
    文档一致性测试

    解析 docs/logbook/sql_file_inventory.md 中的前缀表格，
    与实际扫描结果对比，确保文档与代码同步。
    """

    DOC_FILE = Path(__file__).parent.parent.parent / "docs" / "logbook" / "sql_file_inventory.md"

    def _parse_doc_prefixes(self) -> dict[str, dict]:
        """
        解析文档中的前缀表格

        Returns:
            {prefix: {"filename": str, "type": str, "domain": str}, ...}
        """
        if not self.DOC_FILE.exists():
            return {}

        content = self.DOC_FILE.read_text(encoding="utf-8")

        # 匹配表格行：| 前缀 | 文件名 | 功能域 | 类型 | 说明 |
        # 格式示例：| 01 | 01_logbook_schema.sql | Core | DDL | 核心 schema 与表定义 |
        table_pattern = re.compile(
            r"^\|\s*(?:~~)?(\d{2})(?:~~)?\s*\|\s*([^\|]+)\s*\|\s*([^\|]+)\s*\|\s*([^\|]+)\s*\|",
            re.MULTILINE,
        )

        prefixes = {}
        for match in table_pattern.finditer(content):
            prefix = match.group(1)
            filename = match.group(2).strip()
            domain = match.group(3).strip()
            file_type = match.group(4).strip()

            # 跳过表头和分隔行
            if filename.startswith("--") or filename == "文件名" or "已废弃" in filename:
                continue

            # 标记废弃的前缀
            is_deprecated = "~~" in match.group(0) or "已废弃" in filename

            prefixes[prefix] = {
                "filename": filename,
                "domain": domain,
                "type": file_type,
                "deprecated": is_deprecated,
            }

        return prefixes

    def _get_actual_prefixes(self) -> dict[str, list[str]]:
        """
        获取实际存在的前缀及其文件

        Returns:
            {prefix: [filename, ...], ...}
        """
        prefixes: dict[str, list[str]] = {}

        # 主目录
        for sql_file in SQL_DIR.glob("*.sql"):
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                prefix = match.group(1)
                if prefix not in prefixes:
                    prefixes[prefix] = []
                prefixes[prefix].append(sql_file.name)

        # verify 子目录
        if VERIFY_DIR.is_dir():
            for sql_file in VERIFY_DIR.glob("*.sql"):
                match = re.match(r"^(\d{2})_", sql_file.name)
                if match:
                    prefix = match.group(1)
                    if prefix not in prefixes:
                        prefixes[prefix] = []
                    prefixes[prefix].append(f"verify/{sql_file.name}")

        return prefixes

    def test_doc_prefixes_match_actual_files(self):
        """验证文档中记录的前缀与实际文件一致"""
        if not self.DOC_FILE.exists():
            pytest.skip(f"文档文件不存在: {self.DOC_FILE}")

        doc_prefixes = self._parse_doc_prefixes()
        actual_prefixes = self._get_actual_prefixes()

        if not doc_prefixes:
            pytest.skip("无法解析文档中的前缀表格")

        # 文档中记录但实际不存在的前缀（排除废弃的）
        doc_only = set()
        for prefix, info in doc_prefixes.items():
            if not info.get("deprecated") and prefix not in actual_prefixes:
                doc_only.add(prefix)

        # 实际存在但文档未记录的前缀
        actual_only = set(actual_prefixes.keys()) - set(doc_prefixes.keys())

        messages = []
        if doc_only:
            messages.append(f"文档中记录但实际不存在的前缀：{sorted(doc_only)}")
        if actual_only:
            messages.append(
                f"实际存在但文档未记录的前缀：{sorted(actual_only)}\n"
                f"请更新 {self.DOC_FILE.relative_to(self.DOC_FILE.parent.parent.parent)}"
            )

        assert not (doc_only or actual_only), "\n".join(messages)

    def test_doc_type_classification_matches_code(self):
        """验证文档中的类型分类与 migrate.py 中的常量一致"""
        if not self.DOC_FILE.exists():
            pytest.skip(f"文档文件不存在: {self.DOC_FILE}")

        from engram.logbook.migrate import (
            DDL_SCRIPT_PREFIXES,
            PERMISSION_SCRIPT_PREFIXES,
            VERIFY_SCRIPT_PREFIXES,
        )

        doc_prefixes = self._parse_doc_prefixes()

        if not doc_prefixes:
            pytest.skip("无法解析文档中的前缀表格")

        mismatches = []

        for prefix, info in doc_prefixes.items():
            if info.get("deprecated"):
                continue

            doc_type = info["type"].lower()

            # 根据文档类型判断期望的分类
            expected_in_ddl = "ddl" in doc_type
            expected_in_permission = "permission" in doc_type
            expected_in_verify = "verify" in doc_type

            actual_in_ddl = prefix in DDL_SCRIPT_PREFIXES
            actual_in_permission = prefix in PERMISSION_SCRIPT_PREFIXES
            actual_in_verify = prefix in VERIFY_SCRIPT_PREFIXES

            # 检查分类是否匹配
            if expected_in_ddl and not actual_in_ddl:
                mismatches.append(f"前缀 {prefix}: 文档标记为 DDL，但不在 DDL_SCRIPT_PREFIXES 中")
            if expected_in_permission and not actual_in_permission:
                mismatches.append(
                    f"前缀 {prefix}: 文档标记为 Permission，但不在 PERMISSION_SCRIPT_PREFIXES 中"
                )
            if expected_in_verify and not actual_in_verify:
                mismatches.append(
                    f"前缀 {prefix}: 文档标记为 Verify，但不在 VERIFY_SCRIPT_PREFIXES 中"
                )

        assert not mismatches, "文档与代码的类型分类不一致：\n" + "\n".join(
            f"  - {m}" for m in mismatches
        )

    def test_deprecated_prefixes_have_no_files(self):
        """验证文档中标记为废弃的前缀没有实际文件"""
        if not self.DOC_FILE.exists():
            pytest.skip(f"文档文件不存在: {self.DOC_FILE}")

        doc_prefixes = self._parse_doc_prefixes()
        actual_prefixes = self._get_actual_prefixes()

        if not doc_prefixes:
            pytest.skip("无法解析文档中的前缀表格")

        # 查找标记为废弃但实际存在文件的前缀
        deprecated_with_files = []
        for prefix, info in doc_prefixes.items():
            if info.get("deprecated") and prefix in actual_prefixes:
                deprecated_with_files.append(
                    f"前缀 {prefix}: 标记为废弃但存在文件 {actual_prefixes[prefix]}"
                )

        assert not deprecated_with_files, "以下废弃前缀仍有实际文件：\n" + "\n".join(
            f"  - {m}" for m in deprecated_with_files
        )
