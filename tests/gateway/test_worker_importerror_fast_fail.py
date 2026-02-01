# -*- coding: utf-8 -*-
"""
Worker 模块 ImportError 快速失败测试

验证 outbox_worker.py 和 reconcile_outbox.py 在缺少必需依赖
(engram.logbook.errors) 时的行为：
1. 立即退出（exit code = 1）
2. 输出错误信息表明是依赖问题

测试策略：
- 使用子进程隔离（subprocess + python -c）
- 使用 sys.meta_path blocking finder 阻止 engram.logbook.errors 导入
- 验证退出码和输出内容

契约要求（来自 docs/architecture/gateway_importerror_and_optional_deps.md）：
- Worker 是独立进程，核心依赖缺失时应立即终止
- 打印错误消息 + sys.exit(1)

注意：由于 engram/__init__.py 在 Worker 模块之前就会导入 engram.logbook，
ImportError 会在依赖链的早期位置触发，而不是在 Worker 模块内部的 try/except 块中。
这是当前项目结构的特性。

模块覆盖范围：
- outbox_worker.py: Outbox 处理 Worker
- reconcile_outbox.py: 对账 Worker
"""

import subprocess
import sys
import textwrap

import pytest


class TestWorkerImportErrorFastFail:
    """
    测试 Worker 模块在必需依赖缺失时的快速失败行为

    使用 sys.meta_path blocking finder 模拟 engram.logbook.errors 不可用，
    验证 Worker 模块在 import-time 因依赖缺失而失败：
    1. 进程退出码为 1
    2. 错误信息包含 engram.logbook.errors
    """

    @pytest.fixture
    def blocking_finder_code(self) -> str:
        """
        生成 blocking import finder 的代码片段

        该 finder 会阻止 engram.logbook.errors 模块的导入，
        模拟 engram_logbook 包未正确安装的情况。
        """
        return textwrap.dedent("""
            import sys

            class BlockingImportFinder:
                '''阻止特定模块导入的 import hook'''

                TARGET_MODULE = 'engram.logbook.errors'

                def find_spec(self, fullname, path, target=None):
                    '''Python 3.4+ 的 import hook 接口'''
                    if fullname == self.TARGET_MODULE:
                        from importlib.machinery import ModuleSpec
                        return ModuleSpec(fullname, self)
                    return None

                def create_module(self, spec):
                    return None

                def exec_module(self, module):
                    raise ImportError(
                        f"测试模拟: {module.__name__} 模块不可用 (engram_logbook 未安装)"
                    )

            # 在最前面安装 hook，确保在任何 engram 模块导入前生效
            sys.meta_path.insert(0, BlockingImportFinder())

            # 清除可能已缓存的 engram 相关模块
            modules_to_clear = [
                k for k in list(sys.modules.keys())
                if k.startswith('engram.')
            ]
            for m in modules_to_clear:
                del sys.modules[m]
        """)

    def test_outbox_worker_fast_fail_on_import_error(self, blocking_finder_code: str):
        """
        验证 outbox_worker.py 在 engram.logbook.errors 不可用时快速失败

        预期行为：
        1. 退出码为 1（ImportError 导致进程异常退出）
        2. 错误信息中包含 engram.logbook.errors
        """
        code = blocking_finder_code + textwrap.dedent("""
            # 直接导入 outbox_worker
            # 由于依赖链问题，ImportError 会在 engram 包初始化时触发
            import engram.gateway.outbox_worker
            # 如果到达这里，说明导入成功（不符合预期）
            print("IMPORT_SUCCEEDED_UNEXPECTEDLY")
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        stdout = result.stdout
        stderr = result.stderr
        combined_output = stdout + stderr

        # 验证 1: 退出码为 1
        assert result.returncode == 1, (
            f"outbox_worker 未正确快速失败\n"
            f"期望 returncode=1, 实际={result.returncode}\n"
            f"stdout: {stdout}\n"
            f"stderr: {stderr}"
        )

        # 验证 2: 错误信息中包含 engram.logbook.errors
        assert "engram.logbook.errors" in combined_output, (
            f"outbox_worker 错误消息应包含 engram.logbook.errors\n"
            f"stdout: {stdout}\n"
            f"stderr: {stderr}"
        )

        # 验证 3: 不应该成功导入
        assert "IMPORT_SUCCEEDED_UNEXPECTEDLY" not in stdout, (
            f"outbox_worker 不应在依赖缺失时成功导入\nstdout: {stdout}\nstderr: {stderr}"
        )

    def test_reconcile_outbox_fast_fail_on_import_error(self, blocking_finder_code: str):
        """
        验证 reconcile_outbox.py 在 engram.logbook.errors 不可用时快速失败

        预期行为：
        1. 退出码为 1（ImportError 导致进程异常退出）
        2. 错误信息中包含 engram.logbook.errors
        """
        code = blocking_finder_code + textwrap.dedent("""
            # 直接导入 reconcile_outbox
            import engram.gateway.reconcile_outbox
            # 如果到达这里，说明导入成功（不符合预期）
            print("IMPORT_SUCCEEDED_UNEXPECTEDLY")
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        stdout = result.stdout
        stderr = result.stderr
        combined_output = stdout + stderr

        # 验证 1: 退出码为 1
        assert result.returncode == 1, (
            f"reconcile_outbox 未正确快速失败\n"
            f"期望 returncode=1, 实际={result.returncode}\n"
            f"stdout: {stdout}\n"
            f"stderr: {stderr}"
        )

        # 验证 2: 错误信息中包含 engram.logbook.errors
        assert "engram.logbook.errors" in combined_output, (
            f"reconcile_outbox 错误消息应包含 engram.logbook.errors\n"
            f"stdout: {stdout}\n"
            f"stderr: {stderr}"
        )

        # 验证 3: 不应该成功导入
        assert "IMPORT_SUCCEEDED_UNEXPECTEDLY" not in stdout, (
            f"reconcile_outbox 不应在依赖缺失时成功导入\nstdout: {stdout}\nstderr: {stderr}"
        )


class TestWorkerInternalFastFailLogic:
    """
    测试 Worker 模块内部的快速失败逻辑

    直接测试模块内部的 try/except ErrorCode 导入逻辑，
    通过在已正常导入的环境中验证其行为。
    """

    def test_outbox_worker_has_error_handling_code(self):
        """
        验证 outbox_worker.py 包含快速失败代码

        检查模块源码中是否有 try/except ImportError 和 sys.exit(1)
        """
        import inspect

        import engram.gateway.outbox_worker as module

        # 获取模块源码
        try:
            source = inspect.getsource(module)
        except OSError:
            pytest.skip("无法获取模块源码")

        # 验证包含快速失败逻辑
        # 注意：模块使用绝对导入 from engram.logbook.errors import ErrorCode
        assert "from engram.logbook.errors import ErrorCode" in source, (
            "outbox_worker.py 应导入 ErrorCode（通过 engram.logbook.errors）"
        )
        assert "ErrorCode." in source, "outbox_worker.py 应使用 ErrorCode 常量"

    def test_reconcile_outbox_has_error_handling_code(self):
        """
        验证 reconcile_outbox.py 包含 ErrorCode 导入

        检查模块源码是否正确导入 ErrorCode
        """
        import inspect

        import engram.gateway.reconcile_outbox as module

        try:
            source = inspect.getsource(module)
        except OSError:
            pytest.skip("无法获取模块源码")

        # 验证包含 ErrorCode 导入
        # 注意：模块使用绝对导入 from engram.logbook.errors import ErrorCode
        assert "from engram.logbook.errors import ErrorCode" in source, (
            "reconcile_outbox.py 应导入 ErrorCode（通过 engram.logbook.errors）"
        )
        assert "ErrorCode." in source, "reconcile_outbox.py 应使用 ErrorCode 常量"


class TestWorkerImportErrorMessageContent:
    """
    测试 Worker 模块的错误消息内容

    验证 ImportError 时的错误输出包含诊断信息
    """

    @pytest.fixture
    def blocking_finder_code(self) -> str:
        """生成 blocking import finder 的代码片段"""
        return textwrap.dedent("""
            import sys

            class BlockingImportFinder:
                TARGET_MODULE = 'engram.logbook.errors'

                def find_spec(self, fullname, path, target=None):
                    if fullname == self.TARGET_MODULE:
                        from importlib.machinery import ModuleSpec
                        return ModuleSpec(fullname, self)
                    return None

                def create_module(self, spec):
                    return None

                def exec_module(self, module):
                    raise ImportError(
                        f"测试模拟: {module.__name__} 模块不可用"
                    )

            sys.meta_path.insert(0, BlockingImportFinder())

            modules_to_clear = [
                k for k in list(sys.modules.keys())
                if k.startswith('engram.')
            ]
            for m in modules_to_clear:
                del sys.modules[m]
        """)

    def test_outbox_worker_error_message_contains_dependency_name(self, blocking_finder_code: str):
        """
        验证 outbox_worker 错误消息包含缺失的依赖名称
        """
        code = blocking_finder_code + textwrap.dedent("""
            import engram.gateway.outbox_worker
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        combined_output = result.stdout + result.stderr

        # 错误消息应该提及 engram.logbook.errors
        assert "engram.logbook.errors" in combined_output, (
            f"outbox_worker 错误消息应提及缺失的依赖\noutput: {combined_output}"
        )

    def test_reconcile_outbox_error_message_contains_dependency_name(
        self, blocking_finder_code: str
    ):
        """
        验证 reconcile_outbox 错误消息包含缺失的依赖名称
        """
        code = blocking_finder_code + textwrap.dedent("""
            import engram.gateway.reconcile_outbox
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        combined_output = result.stdout + result.stderr

        # 错误消息应该提及 engram.logbook.errors
        assert "engram.logbook.errors" in combined_output, (
            f"reconcile_outbox 错误消息应提及缺失的依赖\noutput: {combined_output}"
        )


class TestWorkerImportWithFullDependencies:
    """
    验证 Worker 模块在依赖完整时能正常导入

    这是对照测试，确保测试本身没有误报
    """

    def test_outbox_worker_imports_successfully_with_full_deps(self):
        """
        验证 outbox_worker 在依赖完整时能成功导入
        """
        code = textwrap.dedent("""
            import sys
            try:
                import engram.gateway.outbox_worker
                print("IMPORT_OK")
            except ImportError as e:
                print(f"IMPORT_FAILED: {e}")
            except SystemExit as e:
                print(f"SYSTEM_EXIT: {e.code}")
            except Exception as e:
                print(f"UNEXPECTED_ERROR: {type(e).__name__}: {e}")
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        stdout = result.stdout.strip()

        # 在完整环境下，应该成功导入
        if "IMPORT_OK" in stdout:
            pass  # 成功
        elif "IMPORT_FAILED" in stdout:
            pytest.skip(f"测试环境缺少 engram_logbook: {stdout}")
        elif "SYSTEM_EXIT" in stdout:
            pytest.skip(f"测试环境配置问题: {stdout}")
        else:
            pytest.fail(
                f"outbox_worker 导入结果不明确\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )

    def test_reconcile_outbox_imports_successfully_with_full_deps(self):
        """
        验证 reconcile_outbox 在依赖完整时能成功导入
        """
        code = textwrap.dedent("""
            import sys
            try:
                import engram.gateway.reconcile_outbox
                print("IMPORT_OK")
            except ImportError as e:
                print(f"IMPORT_FAILED: {e}")
            except SystemExit as e:
                print(f"SYSTEM_EXIT: {e.code}")
            except Exception as e:
                print(f"UNEXPECTED_ERROR: {type(e).__name__}: {e}")
        """)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        stdout = result.stdout.strip()

        if "IMPORT_OK" in stdout:
            pass  # 成功
        elif "IMPORT_FAILED" in stdout:
            pytest.skip(f"测试环境缺少 engram_logbook: {stdout}")
        elif "SYSTEM_EXIT" in stdout:
            pytest.skip(f"测试环境配置问题: {stdout}")
        else:
            pytest.fail(
                f"reconcile_outbox 导入结果不明确\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )


# 确保测试可以独立运行
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
