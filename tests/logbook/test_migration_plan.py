# -*- coding: utf-8 -*-
"""
测试迁移计划生成功能

验证:
1. generate_migration_plan() 输出结构稳定
2. 不同参数组合产生预期的分类结果
3. 不连接数据库即可执行
"""

import sys
from pathlib import Path

# 确保可以导入 engram
scripts_dir = Path(__file__).parent.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))


class TestGenerateMigrationPlan:
    """测试迁移计划生成函数"""

    def test_plan_output_structure_keys(self):
        """验证输出结构包含所有必需的键"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        # 必需的顶级键
        required_keys = {
            "ok",
            "plan_mode",
            "sql_dir",
            "ddl",
            "permissions",
            "verify",
            "execute",
            "duplicates",
            "flags",
            "script_prefixes",
            "summary",
        }

        assert required_keys.issubset(result.keys()), (
            f"缺少必需的键: {required_keys - result.keys()}"
        )

    def test_plan_mode_flag_is_true(self):
        """验证 plan_mode 始终为 True"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        assert result["plan_mode"] is True

    def test_flags_structure(self):
        """验证 flags 字段结构正确"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(
            apply_roles=True,
            apply_openmemory_grants=True,
            verify=True,
            do_precheck=False,
        )

        flags = result["flags"]
        assert "apply_roles" in flags
        assert "apply_openmemory_grants" in flags
        assert "verify" in flags

        # 验证值与参数一致
        assert flags["apply_roles"] is True
        assert flags["apply_openmemory_grants"] is True
        assert flags["verify"] is True

    def test_script_prefixes_structure(self):
        """验证 script_prefixes 字段结构正确"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        prefixes = result["script_prefixes"]
        assert "ddl" in prefixes
        assert "permissions" in prefixes
        assert "verify" in prefixes

        # 验证是列表类型
        assert isinstance(prefixes["ddl"], list)
        assert isinstance(prefixes["permissions"], list)
        assert isinstance(prefixes["verify"], list)

        # 验证包含已知前缀
        assert "01" in prefixes["ddl"]
        assert "04" in prefixes["permissions"]
        assert "99" in prefixes["verify"]

    def test_summary_structure(self):
        """验证 summary 字段结构正确"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        summary = result["summary"]
        required_summary_keys = {
            "total_files",
            "ddl_count",
            "permissions_count",
            "verify_count",
            "execute_count",
            "duplicate_prefixes",
        }

        assert required_summary_keys.issubset(summary.keys()), (
            f"summary 缺少必需的键: {required_summary_keys - summary.keys()}"
        )

        # 验证是整数类型
        assert isinstance(summary["total_files"], int)
        assert isinstance(summary["ddl_count"], int)
        assert isinstance(summary["permissions_count"], int)
        assert isinstance(summary["verify_count"], int)
        assert isinstance(summary["execute_count"], int)
        assert isinstance(summary["duplicate_prefixes"], list)

    def test_ddl_permissions_verify_are_lists_of_strings(self):
        """验证 ddl/permissions/verify/execute 是字符串列表"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        for key in ["ddl", "permissions", "verify", "execute"]:
            assert isinstance(result[key], list), f"{key} 应该是列表"
            for item in result[key]:
                assert isinstance(item, str), f"{key} 中的项应该是字符串"

    def test_execute_list_changes_with_flags(self):
        """验证 execute 列表根据开关变化"""
        from engram.logbook.migrate import generate_migration_plan

        # 默认情况（仅 DDL）
        result_default = generate_migration_plan(do_precheck=False)
        execute_default_count = result_default["summary"]["execute_count"]

        # 包含 roles
        result_roles = generate_migration_plan(apply_roles=True, do_precheck=False)
        execute_roles_count = result_roles["summary"]["execute_count"]

        # 包含 verify
        result_verify = generate_migration_plan(verify=True, do_precheck=False)
        execute_verify_count = result_verify["summary"]["execute_count"]

        # 验证 execute_count 随开关增加
        # 注意：如果权限脚本存在，roles 会增加计数
        assert execute_roles_count >= execute_default_count
        assert execute_verify_count >= execute_default_count

    def test_sql_dir_is_absolute_path(self):
        """验证 sql_dir 是绝对路径"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        sql_dir = result["sql_dir"]
        assert Path(sql_dir).is_absolute(), "sql_dir 应该是绝对路径"

    def test_duplicates_is_dict(self):
        """验证 duplicates 是字典"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        assert isinstance(result["duplicates"], dict)

    def test_ok_is_true_when_sql_dir_exists(self):
        """验证当 SQL 目录存在时 ok 为 True"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        assert result["ok"] is True

    def test_ok_is_false_when_sql_dir_not_exists(self):
        """验证当 SQL 目录不存在时 ok 为 False"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(
            sql_dir=Path("/nonexistent/path/to/sql"),
            do_precheck=False,
        )

        assert result["ok"] is False
        assert "code" in result
        assert result["code"] == "SQL_DIR_NOT_FOUND"

    def test_precheck_included_when_enabled(self):
        """验证启用预检时包含 precheck 字段"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=True)

        assert "precheck" in result
        assert "ok" in result["precheck"]
        assert "checks" in result["precheck"]

    def test_precheck_not_included_when_disabled(self):
        """验证禁用预检时不包含 precheck 字段"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        # do_precheck=False 时不应有 precheck 字段
        assert "precheck" not in result


class TestMigrationPlanConsistency:
    """测试迁移计划的一致性"""

    def test_summary_counts_match_list_lengths(self):
        """验证 summary 中的计数与列表长度一致"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(do_precheck=False)

        summary = result["summary"]
        assert summary["ddl_count"] == len(result["ddl"])
        assert summary["permissions_count"] == len(result["permissions"])
        assert summary["verify_count"] == len(result["verify"])
        assert summary["execute_count"] == len(result["execute"])

    def test_execute_subset_of_all_files(self):
        """验证 execute 是所有文件的子集"""
        from engram.logbook.migrate import generate_migration_plan

        result = generate_migration_plan(
            apply_roles=True,
            apply_openmemory_grants=True,
            verify=True,
            do_precheck=False,
        )

        all_files = set(result["ddl"] + result["permissions"] + result["verify"])
        execute_files = set(result["execute"])

        assert execute_files.issubset(all_files), (
            f"execute 包含未分类的文件: {execute_files - all_files}"
        )

    def test_script_prefixes_match_constants(self):
        """验证 script_prefixes 与模块常量一致"""
        from engram.logbook.migrate import (
            DDL_SCRIPT_PREFIXES,
            PERMISSION_SCRIPT_PREFIXES,
            VERIFY_SCRIPT_PREFIXES,
            generate_migration_plan,
        )

        result = generate_migration_plan(do_precheck=False)

        prefixes = result["script_prefixes"]
        assert set(prefixes["ddl"]) == DDL_SCRIPT_PREFIXES
        assert set(prefixes["permissions"]) == PERMISSION_SCRIPT_PREFIXES
        assert set(prefixes["verify"]) == VERIFY_SCRIPT_PREFIXES
