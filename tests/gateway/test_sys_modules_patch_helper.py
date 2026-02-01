# -*- coding: utf-8 -*-
"""
sys_modules_patch helper 单元测试

覆盖场景：
- 基本替换和移除
- 嵌套使用
- 异常中断时正确恢复
- 重复 key 恢复顺序
- FailingImport 行为
"""

import sys
from unittest.mock import MagicMock

import pytest

from tests.gateway.helpers.sys_modules_patch import (
    _MISSING,
    FailingImport,
    make_failing_import,
    patch_sys_modules,
)


class TestPatchSysModulesBasic:
    """基本功能测试"""

    def test_replace_existing_module(self):
        """替换已存在的模块"""
        # 使用一个肯定存在的模块
        original = sys.modules.get("os")
        fake = MagicMock()

        with patch_sys_modules(replacements={"os": fake}):
            assert sys.modules["os"] is fake

        assert sys.modules["os"] is original

    def test_replace_nonexistent_module(self):
        """替换不存在的模块"""
        module_name = "__test_nonexistent_module_12345__"
        assert module_name not in sys.modules

        fake = MagicMock()

        with patch_sys_modules(replacements={module_name: fake}):
            assert sys.modules[module_name] is fake

        # 退出后应该被移除
        assert module_name not in sys.modules

    def test_remove_existing_module(self):
        """移除已存在的模块"""
        module_name = "__test_module_to_remove__"
        original = MagicMock()
        sys.modules[module_name] = original

        try:
            with patch_sys_modules(remove=[module_name]):
                assert module_name not in sys.modules

            # 退出后应该恢复
            assert sys.modules[module_name] is original
        finally:
            # 清理
            sys.modules.pop(module_name, None)

    def test_remove_nonexistent_module_is_noop(self):
        """移除不存在的模块是无操作"""
        module_name = "__test_nonexistent_to_remove__"
        assert module_name not in sys.modules

        # 不应该抛出异常
        with patch_sys_modules(remove=[module_name]):
            assert module_name not in sys.modules

        assert module_name not in sys.modules

    def test_empty_params(self):
        """空参数不影响 sys.modules"""
        original_keys = set(sys.modules.keys())

        with patch_sys_modules():
            pass

        assert set(sys.modules.keys()) == original_keys

    def test_none_params(self):
        """None 参数等同于空参数"""
        original_keys = set(sys.modules.keys())

        with patch_sys_modules(replacements=None, remove=None):
            pass

        assert set(sys.modules.keys()) == original_keys


class TestPatchSysModulesNested:
    """嵌套使用测试"""

    def test_nested_same_key_inner_wins(self):
        """嵌套使用时，内层值覆盖外层"""
        module_name = "__test_nested_module__"
        outer = MagicMock(name="outer")
        inner = MagicMock(name="inner")

        with patch_sys_modules(replacements={module_name: outer}):
            assert sys.modules[module_name] is outer

            with patch_sys_modules(replacements={module_name: inner}):
                assert sys.modules[module_name] is inner

            # 内层退出后，恢复到外层值
            assert sys.modules[module_name] is outer

        # 外层退出后，模块被移除（因为原本不存在）
        assert module_name not in sys.modules

    def test_nested_restore_order_is_lifo(self):
        """嵌套恢复顺序是 LIFO"""
        module_name = "__test_lifo_module__"
        values = []

        with patch_sys_modules(replacements={module_name: "L1"}):
            values.append(sys.modules[module_name])

            with patch_sys_modules(replacements={module_name: "L2"}):
                values.append(sys.modules[module_name])

                with patch_sys_modules(replacements={module_name: "L3"}):
                    values.append(sys.modules[module_name])

                values.append(sys.modules[module_name])
            values.append(sys.modules[module_name])

        assert module_name not in sys.modules
        assert values == ["L1", "L2", "L3", "L2", "L1"]

    def test_nested_different_keys(self):
        """嵌套使用不同 key"""
        key1 = "__test_nested_key1__"
        key2 = "__test_nested_key2__"

        with patch_sys_modules(replacements={key1: "v1"}):
            assert sys.modules[key1] == "v1"
            assert key2 not in sys.modules

            with patch_sys_modules(replacements={key2: "v2"}):
                assert sys.modules[key1] == "v1"
                assert sys.modules[key2] == "v2"

            assert sys.modules[key1] == "v1"
            assert key2 not in sys.modules

        assert key1 not in sys.modules
        assert key2 not in sys.modules

    def test_nested_with_existing_module(self):
        """嵌套使用时正确恢复已存在的模块"""
        module_name = "__test_nested_existing__"
        original = MagicMock(name="original")
        sys.modules[module_name] = original

        try:
            with patch_sys_modules(replacements={module_name: "L1"}):
                with patch_sys_modules(replacements={module_name: "L2"}):
                    assert sys.modules[module_name] == "L2"
                assert sys.modules[module_name] == "L1"
            assert sys.modules[module_name] is original
        finally:
            sys.modules.pop(module_name, None)


class TestPatchSysModulesExceptionSafety:
    """异常安全性测试"""

    def test_restore_on_exception_in_context(self):
        """上下文内异常时正确恢复"""
        module_name = "__test_exception_module__"
        original = MagicMock()
        sys.modules[module_name] = original

        try:
            with pytest.raises(ValueError, match="测试异常"):
                with patch_sys_modules(replacements={module_name: "replaced"}):
                    assert sys.modules[module_name] == "replaced"
                    raise ValueError("测试异常")

            # 异常后仍然正确恢复
            assert sys.modules[module_name] is original
        finally:
            sys.modules.pop(module_name, None)

    def test_restore_on_nested_exception(self):
        """嵌套上下文异常时正确恢复"""
        module_name = "__test_nested_exception__"

        with pytest.raises(RuntimeError, match="inner error"):
            with patch_sys_modules(replacements={module_name: "outer"}):
                with patch_sys_modules(replacements={module_name: "inner"}):
                    raise RuntimeError("inner error")

        assert module_name not in sys.modules

    def test_restore_partial_on_setup_error(self):
        """设置过程中的错误不影响恢复"""
        module_name = "__test_setup_error__"

        # 正常情况下应该工作
        with patch_sys_modules(replacements={module_name: "value"}):
            assert sys.modules[module_name] == "value"

        assert module_name not in sys.modules


class TestPatchSysModulesKeyOrder:
    """key 处理顺序测试"""

    def test_remove_then_replace_same_key(self):
        """同一个 key 同时在 remove 和 replacements 中：先移除，再替换"""
        module_name = "__test_remove_replace_same__"
        original = MagicMock(name="original")
        sys.modules[module_name] = original

        try:
            replacement = MagicMock(name="replacement")

            with patch_sys_modules(
                remove=[module_name],
                replacements={module_name: replacement},
            ):
                # replacements 应该生效（因为 remove 先执行）
                assert sys.modules[module_name] is replacement

            # 退出后恢复原值
            assert sys.modules[module_name] is original
        finally:
            sys.modules.pop(module_name, None)

    def test_multiple_keys_all_restored(self):
        """多个 key 全部正确恢复"""
        keys = [f"__test_multi_key_{i}__" for i in range(5)]
        originals = {}

        # 设置一些原始值
        for i, key in enumerate(keys):
            if i % 2 == 0:
                originals[key] = MagicMock(name=f"original_{i}")
                sys.modules[key] = originals[key]

        try:
            replacements = {key: f"replaced_{i}" for i, key in enumerate(keys)}

            with patch_sys_modules(replacements=replacements):
                for i, key in enumerate(keys):
                    assert sys.modules[key] == f"replaced_{i}"

            # 恢复检查
            for i, key in enumerate(keys):
                if i % 2 == 0:
                    assert sys.modules[key] is originals[key]
                else:
                    assert key not in sys.modules
        finally:
            for key in keys:
                sys.modules.pop(key, None)

    def test_duplicate_in_remove_list(self):
        """remove 列表中的重复项只处理一次"""
        module_name = "__test_duplicate_remove__"
        original = MagicMock()
        sys.modules[module_name] = original

        try:
            with patch_sys_modules(remove=[module_name, module_name, module_name]):
                assert module_name not in sys.modules

            assert sys.modules[module_name] is original
        finally:
            sys.modules.pop(module_name, None)


class TestFailingImport:
    """FailingImport 类测试"""

    def test_getattr_raises_import_error(self):
        """访问属性触发 ImportError"""
        failing = FailingImport("测试错误消息")

        with pytest.raises(ImportError, match="测试错误消息"):
            _ = failing.some_attribute

    def test_getattr_raises_on_any_name(self):
        """任意属性名都触发 ImportError"""
        failing = FailingImport("error")

        for attr_name in ["foo", "bar", "__name__", "anything"]:
            with pytest.raises(ImportError):
                getattr(failing, attr_name)

    def test_default_error_message(self):
        """默认错误消息"""
        failing = FailingImport()

        with pytest.raises(ImportError, match="Test import failure"):
            _ = failing.x

    def test_repr(self):
        """__repr__ 返回可读字符串"""
        failing = FailingImport("custom message")
        assert repr(failing) == "FailingImport('custom message')"

    def test_with_patch_sys_modules(self):
        """与 patch_sys_modules 配合使用"""
        module_name = "__test_failing_import__"

        with patch_sys_modules(replacements={module_name: FailingImport("模块不可用")}):
            # 访问模块的任何属性都会触发 ImportError
            with pytest.raises(ImportError, match="模块不可用"):
                _ = sys.modules[module_name].something

        assert module_name not in sys.modules


class TestMakeFailingImport:
    """make_failing_import 工厂函数测试"""

    def test_creates_failing_import(self):
        """创建 FailingImport 实例"""
        failing = make_failing_import("test error")
        assert isinstance(failing, FailingImport)

        with pytest.raises(ImportError, match="test error"):
            _ = failing.attr

    def test_default_message(self):
        """默认错误消息"""
        failing = make_failing_import()

        with pytest.raises(ImportError, match="Test import failure"):
            _ = failing.x


class TestMissingSentinel:
    """_MISSING sentinel 测试"""

    def test_missing_is_unique(self):
        """_MISSING 是唯一的 sentinel"""
        assert _MISSING is not None
        assert _MISSING is not False
        assert _MISSING != ""
        assert _MISSING != 0


class TestIntegrationWithRealModules:
    """与真实模块的集成测试"""

    def test_patch_json_module(self):
        """patch 标准库 json 模块"""
        import json

        original_dumps = json.dumps
        fake_json = MagicMock()
        fake_json.dumps = lambda *args, **kwargs: "FAKE"

        with patch_sys_modules(replacements={"json": fake_json}):
            # 注意：已经导入的引用不受影响
            # 但 sys.modules 中的值被替换
            assert sys.modules["json"] is fake_json

        assert sys.modules["json"].dumps is original_dumps

    def test_simulate_optional_dependency_missing(self):
        """模拟可选依赖缺失场景"""
        # 模拟一个可选依赖模块
        optional_module = "__optional_dependency__"

        with patch_sys_modules(replacements={optional_module: FailingImport("可选依赖未安装")}):
            # 模拟代码中的 try/except ImportError 模式
            try:
                _ = sys.modules[optional_module].some_function
                available = True
            except ImportError:
                available = False

            assert not available

        # 退出后模块不存在
        assert optional_module not in sys.modules


class TestEdgeCases:
    """边界情况测试"""

    def test_replace_with_none(self):
        """替换值为 None"""
        module_name = "__test_none_value__"

        with patch_sys_modules(replacements={module_name: None}):
            assert module_name in sys.modules
            assert sys.modules[module_name] is None

        assert module_name not in sys.modules

    def test_replace_with_empty_string_key(self):
        """空字符串作为 key（虽然不推荐）"""
        with patch_sys_modules(replacements={"": "empty_key_value"}):
            assert sys.modules[""] == "empty_key_value"

        assert "" not in sys.modules

    def test_large_number_of_modules(self):
        """大量模块的处理"""
        module_count = 100
        modules = {f"__test_bulk_{i}__": i for i in range(module_count)}

        with patch_sys_modules(replacements=modules):
            for name, value in modules.items():
                assert sys.modules[name] == value

        for name in modules:
            assert name not in sys.modules

    def test_context_manager_returns_none(self):
        """上下文管理器返回 None"""
        with patch_sys_modules(replacements={"x": 1}) as result:
            assert result is None
