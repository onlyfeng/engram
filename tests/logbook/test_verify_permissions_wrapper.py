"""
测试 apps/logbook_postgres/sql/99_verify_permissions.sql 是薄包装器而非 SSOT 副本。

SSOT + Wrapper 策略要求：
- SSOT（Single Source of Truth）永远在 sql/verify/99_verify_permissions.sql
- apps/ 下的文件仅为薄包装器，通过 \\i 命令引用 SSOT
- 禁止复制粘贴形成双源
"""

from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent

# 包装器文件路径
WRAPPER_SQL_PATH = PROJECT_ROOT / "apps" / "logbook_postgres" / "sql" / "99_verify_permissions.sql"

# SSOT 文件路径
SSOT_SQL_PATH = PROJECT_ROOT / "sql" / "verify" / "99_verify_permissions.sql"

# 包装器文件体积上限（字节）- 薄包装器不应超过 3KB
WRAPPER_MAX_SIZE_BYTES = 3 * 1024

# 包装器必须包含的 \i 引用命令
EXPECTED_INCLUDE_DIRECTIVE = r"\i ../../../sql/verify/99_verify_permissions.sql"

# SSOT 文件中的关键标记（包装器中不应存在）
SSOT_MARKERS = [
    "DO $$",  # PL/pgSQL 匿名块开始
    "DECLARE",  # 变量声明
    "has_table_privilege",  # 权限检查函数
    "pg_has_role",  # 角色检查函数
    "RAISE NOTICE",  # 输出语句
]


class TestVerifyPermissionsWrapper:
    """验证 apps/ 下的 99_verify_permissions.sql 是薄包装器。"""

    def test_wrapper_file_exists(self):
        """包装器文件必须存在。"""
        assert WRAPPER_SQL_PATH.exists(), f"包装器文件不存在: {WRAPPER_SQL_PATH}"

    def test_ssot_file_exists(self):
        """SSOT 文件必须存在。"""
        assert SSOT_SQL_PATH.exists(), f"SSOT 文件不存在: {SSOT_SQL_PATH}"

    def test_wrapper_contains_include_directive(self):
        """包装器必须包含 \\i 引用 SSOT 的命令。"""
        content = WRAPPER_SQL_PATH.read_text(encoding="utf-8")
        assert EXPECTED_INCLUDE_DIRECTIVE in content, (
            f"包装器缺少 \\i 引用命令。\n"
            f"期望包含: {EXPECTED_INCLUDE_DIRECTIVE}\n"
            f"实际内容:\n{content}"
        )

    def test_wrapper_size_limit(self):
        """包装器文件体积不应超过上限（防止意外复制 SSOT 内容）。"""
        file_size = WRAPPER_SQL_PATH.stat().st_size
        assert file_size <= WRAPPER_MAX_SIZE_BYTES, (
            f"包装器文件体积 {file_size} 字节超过上限 {WRAPPER_MAX_SIZE_BYTES} 字节。\n"
            f"这可能意味着 SSOT 内容被意外复制到了包装器中。\n"
            f"请确保 apps/ 下的文件仅为薄包装器。"
        )

    def test_wrapper_does_not_contain_ssot_logic(self):
        """包装器不应包含 SSOT 中的关键逻辑标记。"""
        content = WRAPPER_SQL_PATH.read_text(encoding="utf-8")
        found_markers = [marker for marker in SSOT_MARKERS if marker in content]
        assert not found_markers, (
            f"包装器包含 SSOT 关键标记，违反 SSOT + Wrapper 策略:\n"
            f"发现的标记: {found_markers}\n"
            f"包装器应只包含 \\i 引用命令，不应复制 SSOT 逻辑。"
        )

    def test_ssot_is_larger_than_wrapper(self):
        """SSOT 文件应比包装器大得多（验证 SSOT 包含实际逻辑）。"""
        wrapper_size = WRAPPER_SQL_PATH.stat().st_size
        ssot_size = SSOT_SQL_PATH.stat().st_size
        # SSOT 应至少是包装器的 2 倍大
        assert ssot_size >= wrapper_size * 2, (
            f"SSOT 文件 ({ssot_size} 字节) 应比包装器 ({wrapper_size} 字节) 大得多。\n"
            f"这可能意味着 SSOT 文件内容被误移动到了其他地方。"
        )
