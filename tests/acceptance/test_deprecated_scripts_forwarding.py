# -*- coding: utf-8 -*-
"""
弃用脚本转发机制验收测试

验证 scripts/ 目录下的弃用入口脚本能正确：
1. 模块可导入且关键符号存在
2. 发出统一格式的弃用警告
3. 成功转发到新的 CLI 入口
4. 弃用提示不包含敏感信息

测试对象:
- scripts/scm_sync_worker.py -> engram.logbook.cli.scm_sync worker
- scripts/scm_sync_reaper.py -> engram.logbook.cli.scm_sync reaper

契约依据:
- docs/architecture/cli_entrypoints.md
- src/engram/logbook/deprecation.py
"""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    pass


# ---------- 路径设置 ----------


@pytest.fixture(scope="module")
def project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent.parent.parent


@pytest.fixture(scope="module")
def scripts_dir(project_root: Path) -> Path:
    """获取 scripts 目录"""
    return project_root / "scripts"


@pytest.fixture(scope="module")
def src_dir(project_root: Path) -> Path:
    """获取 src 目录"""
    return project_root / "src"


# ---------- 敏感信息检查 ----------


# 敏感信息关键词列表（用于检查弃用提示中不应包含的内容）
SENSITIVE_KEYWORDS = [
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "privatekey",
    "credential",
    "auth_token",
    "access_token",
    "bearer",
    # 数据库连接串模式
    "postgres://",
    "postgresql://",
    "mysql://",
    # 常见敏感环境变量值
    "@localhost",
    ":5432/",
]


def contains_sensitive_info(text: str) -> list[str]:
    """检查文本是否包含敏感信息，返回匹配的关键词列表"""
    text_lower = text.lower()
    found = []
    for keyword in SENSITIVE_KEYWORDS:
        if keyword.lower() in text_lower:
            found.append(keyword)
    return found


# ---------- scm_sync_worker.py 测试 ----------


class TestScmSyncWorkerDeprecatedScript:
    """scripts/scm_sync_worker.py 弃用脚本测试"""

    SCRIPT_NAME = "scm_sync_worker.py"
    MODULE_NAME = "scripts.scm_sync_worker"

    # 必须导出的关键符号（向后兼容）
    REQUIRED_SYMBOLS = [
        # 类型
        "SyncExecutorType",
        # 数据类
        "HeartbeatManager",
        # 函数
        "get_db_connection",
        "generate_run_id",
        "read_cursor_before",
        "insert_sync_run_start",
        "insert_sync_run_finish",
        "mark_dead",
        "fail_retry",
        "set_executor",
        "get_executor",
        "execute_sync_job",
        "process_one_job",
        # CLI
        "main",
    ]

    # 兼容别名
    COMPAT_ALIASES = [
        "_get_db_connection",
        "_generate_run_id",
        "_read_cursor_before",
        "_insert_sync_run_start",
        "_insert_sync_run_finish",
    ]

    @pytest.fixture
    def script_path(self, scripts_dir: Path) -> Path:
        """获取脚本路径"""
        path = scripts_dir / self.SCRIPT_NAME
        if not path.exists():
            pytest.skip(f"{self.SCRIPT_NAME} 不存在")
        return path

    def test_module_importable(self, project_root: Path, src_dir: Path) -> None:
        """测试模块可导入"""
        # 确保路径在 sys.path 中
        original_path = sys.path.copy()
        try:
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            # 使用 warnings 捕获弃用警告
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                import scripts.scm_sync_worker as module

            assert module is not None
        finally:
            sys.path[:] = original_path

    def test_required_symbols_exist(self, project_root: Path, src_dir: Path) -> None:
        """测试关键符号存在"""
        original_path = sys.path.copy()
        try:
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                import scripts.scm_sync_worker as module

            # 检查所有必需符号
            missing = []
            for symbol in self.REQUIRED_SYMBOLS:
                if not hasattr(module, symbol):
                    missing.append(symbol)

            assert not missing, f"缺少必需符号: {missing}"

            # 检查兼容别名
            missing_aliases = []
            for alias in self.COMPAT_ALIASES:
                if not hasattr(module, alias):
                    missing_aliases.append(alias)

            assert not missing_aliases, f"缺少兼容别名: {missing_aliases}"
        finally:
            sys.path[:] = original_path

    def test_all_exports_declared(self, project_root: Path, src_dir: Path) -> None:
        """测试 __all__ 包含所有导出符号"""
        original_path = sys.path.copy()
        try:
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                import scripts.scm_sync_worker as module

            assert hasattr(module, "__all__"), "模块缺少 __all__ 声明"

            # 检查必需符号在 __all__ 中
            all_exports = set(module.__all__)
            for symbol in self.REQUIRED_SYMBOLS:
                assert symbol in all_exports, f"符号 {symbol} 不在 __all__ 中"
        finally:
            sys.path[:] = original_path

    def test_help_shows_deprecation_warning(self, script_path: Path, project_root: Path) -> None:
        """测试 --help 显示弃用警告"""
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )

        # --help 应该返回 0（成功）
        assert result.returncode == 0, f"returncode={result.returncode}, stderr: {result.stderr}"

        combined_output = result.stderr + result.stdout

        # 检查弃用警告
        has_deprecation = (
            "DEPRECATED" in combined_output
            or "弃用" in combined_output
            or "deprecated" in combined_output.lower()
        )
        assert has_deprecation, f"未找到弃用警告\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # 检查推荐的新命令
        has_new_command = (
            "engram-scm-worker" in combined_output or "engram-scm-sync worker" in combined_output
        )
        assert has_new_command, f"弃用警告未包含推荐的新命令\nstderr: {result.stderr}"

    def test_deprecation_message_no_sensitive_info(
        self, script_path: Path, project_root: Path
    ) -> None:
        """测试弃用提示不包含敏感信息"""
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )

        combined_output = result.stderr + result.stdout
        sensitive_found = contains_sensitive_info(combined_output)

        assert not sensitive_found, (
            f"弃用提示包含敏感信息: {sensitive_found}\nstderr: {result.stderr}"
        )

    def test_help_output_contains_usage(self, script_path: Path, project_root: Path) -> None:
        """测试 --help 包含使用说明（转发成功）"""
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )

        combined_output = result.stderr + result.stdout

        # 应该有帮助信息
        has_help = (
            "--worker-id" in combined_output
            or "worker" in combined_output.lower()
            or "usage" in combined_output.lower()
        )
        assert has_help, f"帮助输出未包含预期内容\nstdout: {result.stdout}"


# ---------- scm_sync_reaper.py 测试 ----------


class TestScmSyncReaperDeprecatedScript:
    """scripts/scm_sync_reaper.py 弃用脚本测试"""

    SCRIPT_NAME = "scm_sync_reaper.py"
    MODULE_NAME = "scripts.scm_sync_reaper"

    # 必须导出的关键符号（向后兼容）
    REQUIRED_SYMBOLS = [
        # 常量
        "DEFAULT_GRACE_SECONDS",
        "DEFAULT_MAX_DURATION_SECONDS",
        "DEFAULT_RETRY_DELAY_SECONDS",
        "DEFAULT_MAX_REAPER_BACKOFF_SECONDS",
        # 枚举
        "JobRecoveryPolicy",
        # 函数
        "format_error",
        "mark_job_pending",
        "compute_backoff_seconds",
        "process_expired_jobs",
        "process_expired_runs",
        "process_expired_locks",
        "run_reaper",
        # CLI
        "main",
    ]

    # 兼容别名
    COMPAT_ALIASES = [
        "_format_error",
        "_mark_job_pending",
        "_compute_backoff_seconds",
    ]

    @pytest.fixture
    def script_path(self, scripts_dir: Path) -> Path:
        """获取脚本路径"""
        path = scripts_dir / self.SCRIPT_NAME
        if not path.exists():
            pytest.skip(f"{self.SCRIPT_NAME} 不存在")
        return path

    def test_module_importable(self, project_root: Path, src_dir: Path) -> None:
        """测试模块可导入"""
        original_path = sys.path.copy()
        try:
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                import scripts.scm_sync_reaper as module

            assert module is not None
        finally:
            sys.path[:] = original_path

    def test_required_symbols_exist(self, project_root: Path, src_dir: Path) -> None:
        """测试关键符号存在"""
        original_path = sys.path.copy()
        try:
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                import scripts.scm_sync_reaper as module

            # 检查所有必需符号
            missing = []
            for symbol in self.REQUIRED_SYMBOLS:
                if not hasattr(module, symbol):
                    missing.append(symbol)

            assert not missing, f"缺少必需符号: {missing}"

            # 检查兼容别名
            missing_aliases = []
            for alias in self.COMPAT_ALIASES:
                if not hasattr(module, alias):
                    missing_aliases.append(alias)

            assert not missing_aliases, f"缺少兼容别名: {missing_aliases}"
        finally:
            sys.path[:] = original_path

    def test_all_exports_declared(self, project_root: Path, src_dir: Path) -> None:
        """测试 __all__ 包含所有导出符号"""
        original_path = sys.path.copy()
        try:
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                import scripts.scm_sync_reaper as module

            assert hasattr(module, "__all__"), "模块缺少 __all__ 声明"

            # 检查必需符号在 __all__ 中
            all_exports = set(module.__all__)
            for symbol in self.REQUIRED_SYMBOLS:
                assert symbol in all_exports, f"符号 {symbol} 不在 __all__ 中"
        finally:
            sys.path[:] = original_path

    def test_help_shows_deprecation_warning(self, script_path: Path, project_root: Path) -> None:
        """测试 --help 显示弃用警告"""
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )

        # --help 应该返回 0（成功）
        assert result.returncode == 0, f"returncode={result.returncode}, stderr: {result.stderr}"

        combined_output = result.stderr + result.stdout

        # 检查弃用警告
        has_deprecation = (
            "DEPRECATED" in combined_output
            or "弃用" in combined_output
            or "deprecated" in combined_output.lower()
        )
        assert has_deprecation, f"未找到弃用警告\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # 检查推荐的新命令
        has_new_command = (
            "engram-scm-reaper" in combined_output or "engram-scm-sync reaper" in combined_output
        )
        assert has_new_command, f"弃用警告未包含推荐的新命令\nstderr: {result.stderr}"

    def test_deprecation_message_no_sensitive_info(
        self, script_path: Path, project_root: Path
    ) -> None:
        """测试弃用提示不包含敏感信息"""
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )

        combined_output = result.stderr + result.stdout
        sensitive_found = contains_sensitive_info(combined_output)

        assert not sensitive_found, (
            f"弃用提示包含敏感信息: {sensitive_found}\nstderr: {result.stderr}"
        )

    def test_help_output_contains_usage(self, script_path: Path, project_root: Path) -> None:
        """测试 --help 包含使用说明（转发成功）"""
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )

        combined_output = result.stderr + result.stdout

        # 应该有帮助信息
        has_help = (
            "--dry-run" in combined_output
            or "--grace-seconds" in combined_output
            or "reaper" in combined_output.lower()
            or "usage" in combined_output.lower()
        )
        assert has_help, f"帮助输出未包含预期内容\nstdout: {result.stdout}"


# ---------- 统一契约测试 ----------


class TestDeprecationModuleContract:
    """engram.logbook.deprecation 模块契约测试"""

    def test_deprecation_module_importable(self, src_dir: Path) -> None:
        """测试弃用模块可导入"""
        original_path = sys.path.copy()
        try:
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            from engram.logbook.deprecation import (
                DEPRECATION_DOC_URL,
                DEPRECATION_VERSION,
                emit_deprecation_warning,
                emit_import_deprecation_warning,
            )

            assert emit_deprecation_warning is not None
            assert emit_import_deprecation_warning is not None
            assert DEPRECATION_DOC_URL is not None
            assert DEPRECATION_VERSION is not None
        finally:
            sys.path[:] = original_path

    def test_deprecation_warning_format(self, src_dir: Path) -> None:
        """测试弃用警告格式符合契约"""
        original_path = sys.path.copy()
        try:
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            import io
            import sys as _sys

            from engram.logbook.deprecation import (
                DEPRECATION_DOC_URL,
                emit_deprecation_warning,
            )

            # 捕获 stderr
            old_stderr = _sys.stderr
            _sys.stderr = io.StringIO()

            try:
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    emit_deprecation_warning(
                        old_script="test/script.py",
                        new_commands=["new-cmd", "alt-cmd"],
                        package_module="test.module",
                        to_stderr=True,
                    )

                    # 检查 warnings.warn 被调用
                    assert len(w) == 1
                    assert issubclass(w[0].category, DeprecationWarning)

                    # 检查警告消息内容
                    msg = str(w[0].message)
                    assert "test/script.py" in msg
                    assert "new-cmd" in msg
                    assert "alt-cmd" in msg
                    assert DEPRECATION_DOC_URL in msg

                # 检查 stderr 输出
                stderr_output = _sys.stderr.getvalue()
                assert "DEPRECATION WARNING" in stderr_output
                assert "test/script.py" in stderr_output
            finally:
                _sys.stderr = old_stderr
        finally:
            sys.path[:] = original_path

    def test_import_deprecation_warning_format(self, src_dir: Path) -> None:
        """测试导入弃用警告格式"""
        original_path = sys.path.copy()
        try:
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            from engram.logbook.deprecation import emit_import_deprecation_warning

            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                emit_import_deprecation_warning(
                    old_module="old.module",
                    new_module="new.module",
                )

                assert len(w) == 1
                assert issubclass(w[0].category, DeprecationWarning)

                msg = str(w[0].message)
                assert "old.module" in msg
                assert "new.module" in msg
        finally:
            sys.path[:] = original_path


# ---------- 运行入口 ----------


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
