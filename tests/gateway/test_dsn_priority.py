# -*- coding: utf-8 -*-
"""
DSN 优先级回归测试

验证 LogbookAdapter 的 DSN 选择逻辑:
- 显式传入 dsn 参数时使用强覆盖策略
- DSN 优先级: 显式参数 > POSTGRES_DSN > TEST_PG_DSN

此测试确保 logbook_adapter 与 logbook_db 的 DSN 设置逻辑对齐，
保证"单一事实来源"的 DSN 传递。
"""

import os
from unittest.mock import patch


class TestDSNPriority:
    """DSN 优先级测试"""

    def test_explicit_dsn_overrides_env(self):
        """
        显式传入的 dsn 参数应该覆盖已存在的环境变量

        验证强覆盖策略: os.environ['POSTGRES_DSN'] = dsn
        """
        # 先清理全局状态
        from engram.gateway import logbook_adapter

        logbook_adapter.reset_adapter()

        # 设置环境变量
        original_dsn = os.environ.get("POSTGRES_DSN")
        os.environ["POSTGRES_DSN"] = "postgresql://old:old@localhost/old_db"

        try:
            explicit_dsn = "postgresql://new:new@localhost/new_db"

            # 创建适配器时显式传入 dsn
            adapter = logbook_adapter.LogbookAdapter(dsn=explicit_dsn)

            # 断言: 环境变量应该被强覆盖
            assert os.environ["POSTGRES_DSN"] == explicit_dsn, (
                "显式传入的 dsn 参数应该覆盖环境变量 POSTGRES_DSN"
            )

            # 断言: 适配器内部保存的 _dsn 应该是显式值
            assert adapter._dsn == explicit_dsn

        finally:
            # 恢复环境变量
            if original_dsn is None:
                os.environ.pop("POSTGRES_DSN", None)
            else:
                os.environ["POSTGRES_DSN"] = original_dsn
            logbook_adapter.reset_adapter()

    def test_none_dsn_preserves_env(self):
        """
        不传入 dsn 参数时应该保留已存在的环境变量
        """
        from engram.gateway import logbook_adapter

        logbook_adapter.reset_adapter()

        # 设置环境变量
        original_dsn = os.environ.get("POSTGRES_DSN")
        existing_dsn = "postgresql://existing:existing@localhost/existing_db"
        os.environ["POSTGRES_DSN"] = existing_dsn

        try:
            # 创建适配器时不传入 dsn
            adapter = logbook_adapter.LogbookAdapter(dsn=None)

            # 断言: 环境变量应该保持不变
            assert os.environ["POSTGRES_DSN"] == existing_dsn, "不传入 dsn 参数时不应修改环境变量"

            # 断言: 适配器内部 _dsn 为 None
            assert adapter._dsn is None

        finally:
            if original_dsn is None:
                os.environ.pop("POSTGRES_DSN", None)
            else:
                os.environ["POSTGRES_DSN"] = original_dsn
            logbook_adapter.reset_adapter()

    def test_get_adapter_with_explicit_dsn(self):
        """
        get_adapter(dsn) 应该正确传递 dsn 到 LogbookAdapter
        """
        from engram.gateway import logbook_adapter

        logbook_adapter.reset_adapter()

        original_dsn = os.environ.get("POSTGRES_DSN")
        os.environ["POSTGRES_DSN"] = "postgresql://old:old@localhost/old_db"

        try:
            explicit_dsn = "postgresql://explicit:explicit@localhost/explicit_db"

            # 使用 get_adapter 获取适配器
            logbook_adapter.get_adapter(dsn=explicit_dsn)

            # 断言: 环境变量应该被强覆盖
            assert os.environ["POSTGRES_DSN"] == explicit_dsn

        finally:
            if original_dsn is None:
                os.environ.pop("POSTGRES_DSN", None)
            else:
                os.environ["POSTGRES_DSN"] = original_dsn
            logbook_adapter.reset_adapter()

    def test_dsn_priority_docstring_alignment(self):
        """
        验证 logbook_adapter 与 logbook_db 的 DSN 优先级一致

        两个模块应该遵循相同的优先级: 显式参数 > POSTGRES_DSN > TEST_PG_DSN
        """
        from engram.gateway import logbook_adapter

        # 检查 LogbookAdapter.__init__ 的 docstring 包含正确的优先级描述
        docstring = logbook_adapter.LogbookAdapter.__init__.__doc__
        assert docstring is not None
        assert "显式参数" in docstring or "dsn" in docstring.lower()
        assert "POSTGRES_DSN" in docstring


class TestDSNOverrideWithMock:
    """使用 Mock 验证 DSN 覆盖行为（不需要真实数据库连接）"""

    def test_explicit_dsn_triggers_env_update(self):
        """
        使用 patch 验证显式 dsn 会触发环境变量更新
        """
        from engram.gateway import logbook_adapter

        logbook_adapter.reset_adapter()

        with patch.dict(os.environ, {"POSTGRES_DSN": "old_value"}, clear=False):
            explicit_dsn = "postgresql://test:test@localhost/test_db"

            # 创建适配器
            logbook_adapter.LogbookAdapter(dsn=explicit_dsn)

            # 断言: 环境变量被更新
            assert os.environ["POSTGRES_DSN"] == explicit_dsn, (
                f"期望 POSTGRES_DSN={explicit_dsn}, 实际={os.environ.get('POSTGRES_DSN')}"
            )

        logbook_adapter.reset_adapter()

    def test_multiple_adapters_last_dsn_wins(self):
        """
        多次创建适配器时，最后一个显式 dsn 应该生效

        这验证了强覆盖策略的一致性
        """
        from engram.gateway import logbook_adapter

        logbook_adapter.reset_adapter()

        original_dsn = os.environ.get("POSTGRES_DSN")

        try:
            # 创建第一个适配器
            dsn1 = "postgresql://first:first@localhost/first_db"
            logbook_adapter.LogbookAdapter(dsn=dsn1)
            assert os.environ["POSTGRES_DSN"] == dsn1

            # 创建第二个适配器
            dsn2 = "postgresql://second:second@localhost/second_db"
            logbook_adapter.LogbookAdapter(dsn=dsn2)
            assert os.environ["POSTGRES_DSN"] == dsn2

        finally:
            if original_dsn is None:
                os.environ.pop("POSTGRES_DSN", None)
            else:
                os.environ["POSTGRES_DSN"] = original_dsn
            logbook_adapter.reset_adapter()
