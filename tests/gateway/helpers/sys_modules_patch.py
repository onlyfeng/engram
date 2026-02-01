# -*- coding: utf-8 -*-
"""
sys.modules 补丁上下文管理器

提供用于测试 optional-deps/ImportError 场景的工具。

约定:
    所有 gateway optional-deps/ImportError 模拟必须通过 patch_sys_modules() 进行，
    不允许直接写 `sys.modules[...] = ...`。

使用示例:
    from tests.gateway.helpers import patch_sys_modules

    # 基本用法：替换和移除模块
    with patch_sys_modules(
        replacements={"some.module": FakeModule()},
        remove=["another.module"],
    ):
        # 在此上下文中，some.module 被替换，another.module 被移除
        import some.module  # 返回 FakeModule()
        import another.module  # 触发 ImportError

    # 嵌套使用
    with patch_sys_modules(replacements={"a": Mock1()}):
        with patch_sys_modules(replacements={"a": Mock2()}):
            # 此处 sys.modules["a"] == Mock2()
        # 此处 sys.modules["a"] == Mock1()
    # 此处 sys.modules["a"] 恢复原始值

    # 配合 FailingImport 模拟 ImportError
    from tests.gateway.helpers.sys_modules_patch import FailingImport

    with patch_sys_modules(
        replacements={"engram.gateway.evidence_store": FailingImport("模块不可用")},
    ):
        from engram.gateway.evidence_store import something  # 抛出 ImportError
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any, Iterator


class FailingImport:
    """
    模拟导入失败的 mock 对象

    当访问此对象的任何属性时，都会抛出 ImportError。
    用于测试可选依赖缺失时的优雅降级行为。

    使用示例:
        sys.modules["engram.gateway.evidence_store"] = FailingImport("模块不可用")
        # 之后任何对 evidence_store 的属性访问都会触发 ImportError
    """

    def __init__(self, error_message: str = "Test import failure") -> None:
        """
        Args:
            error_message: ImportError 的消息内容
        """
        # 使用 object.__setattr__ 避免触发 __setattr__ 检查
        object.__setattr__(self, "_error_message", error_message)

    def __getattr__(self, name: str) -> Any:
        raise ImportError(self._error_message)

    def __repr__(self) -> str:
        return f"FailingImport({self._error_message!r})"


# 用于标记"原始模块不存在"的 sentinel 值
_MISSING = object()


@contextmanager
def patch_sys_modules(
    replacements: dict[str, object] | None = None,
    remove: list[str] | None = None,
) -> Iterator[None]:
    """
    临时修改 sys.modules 的上下文管理器

    在进入上下文时：
    1. 保存所有将被影响的模块的原始状态（存在或不存在）
    2. 移除 `remove` 列表中的模块（如果存在）
    3. 替换/添加 `replacements` 字典中的模块

    在退出上下文时（包括异常情况）：
    1. 恢复所有受影响模块的原始状态
    2. 原本不存在的模块被删除
    3. 原本存在的模块恢复原值

    Args:
        replacements: 要替换或添加的模块映射，key 为模块名，value 为替换值
        remove: 要移除的模块名列表

    Yields:
        None

    注意:
        - 支持嵌套使用，每层独立保存和恢复状态
        - 异常中断时仍能正确恢复
        - 恢复顺序与操作顺序相反（LIFO），确保嵌套正确性

    使用示例:
        # 移除模块
        with patch_sys_modules(remove=["optional.dep"]):
            try:
                import optional.dep
            except ImportError:
                print("模块已被移除")

        # 替换模块
        fake = type("FakeModule", (), {"attr": "value"})()
        with patch_sys_modules(replacements={"real.module": fake}):
            import real.module
            assert real.module.attr == "value"

        # 同时移除和替换
        with patch_sys_modules(
            replacements={"a": Mock()},
            remove=["b"],
        ):
            pass

        # 嵌套使用
        with patch_sys_modules(replacements={"x": 1}):
            assert sys.modules["x"] == 1
            with patch_sys_modules(replacements={"x": 2}):
                assert sys.modules["x"] == 2
            assert sys.modules["x"] == 1
    """
    replacements = replacements or {}
    remove = remove or []

    # 记录原始状态：(module_name, original_value_or_MISSING)
    # 使用列表保持操作顺序，恢复时按相反顺序进行
    saved_state: list[tuple[str, object]] = []

    # 收集所有受影响的模块名（去重但保持顺序）
    affected_modules: list[str] = []
    seen: set[str] = set()
    for name in remove:
        if name not in seen:
            affected_modules.append(name)
            seen.add(name)
    for name in replacements:
        if name not in seen:
            affected_modules.append(name)
            seen.add(name)

    try:
        # 保存原始状态
        for name in affected_modules:
            if name in sys.modules:
                saved_state.append((name, sys.modules[name]))
            else:
                saved_state.append((name, _MISSING))

        # 执行移除操作
        for name in remove:
            if name in sys.modules:
                del sys.modules[name]

        # 执行替换操作
        for name, value in replacements.items():
            sys.modules[name] = value

        yield

    finally:
        # 按相反顺序恢复原始状态
        for name, original in reversed(saved_state):
            if original is _MISSING:
                # 原本不存在，删除（如果当前存在）
                sys.modules.pop(name, None)
            else:
                # 原本存在，恢复原值
                sys.modules[name] = original


def make_failing_import(error_message: str = "Test import failure") -> FailingImport:
    """
    创建一个 FailingImport 实例的工厂函数

    这是 FailingImport(...) 的便捷别名。

    Args:
        error_message: ImportError 的消息内容

    Returns:
        FailingImport 实例

    使用示例:
        with patch_sys_modules(
            replacements={"foo.bar": make_failing_import("foo.bar 不可用")}
        ):
            pass
    """
    return FailingImport(error_message)
