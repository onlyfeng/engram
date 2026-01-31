# -*- coding: utf-8 -*-
"""
helpers.py 导出的 correlation_id 常量格式验证测试

确保所有导出的 CORR_ID_* 和 TEST_CORRELATION_ID* 常量
都符合 schema 规范: ^corr-[a-fA-F0-9]{16}$

此测试防止再次引入不合规的 correlation_id 常量。
"""

import pytest

from tests.gateway import helpers
from tests.gateway.helpers import CORRELATION_ID_PATTERN, is_valid_correlation_id


# 需要验证格式的常量名称模式
CORRELATION_ID_CONST_PREFIXES = ("TEST_CORRELATION_ID", "CORR_ID_")


class TestHelpersCorrelationIdConstantsFormat:
    """验证 helpers 模块导出的所有 correlation_id 常量格式"""

    def test_all_exported_correlation_id_constants_are_valid(self):
        """所有导出的 CORR_ID_* 和 TEST_CORRELATION_ID* 常量必须符合 schema 格式"""
        invalid_constants = []

        for name in helpers.__all__:
            # 只检查 correlation_id 相关的常量
            if not any(name.startswith(prefix) for prefix in CORRELATION_ID_CONST_PREFIXES):
                continue

            value = getattr(helpers, name, None)

            # 跳过非字符串值（如函数）
            if not isinstance(value, str):
                continue

            if not is_valid_correlation_id(value):
                invalid_constants.append((name, value))

        if invalid_constants:
            msg_lines = ["以下常量不符合 correlation_id 格式 (^corr-[a-fA-F0-9]{16}$):"]
            for name, value in invalid_constants:
                hex_part = value.replace("corr-", "") if value.startswith("corr-") else value
                msg_lines.append(f"  - {name} = {value!r} (hex部分长度={len(hex_part)}, 期望=16)")
            pytest.fail("\n".join(msg_lines))

    @pytest.mark.parametrize(
        "const_name",
        [
            name
            for name in helpers.__all__
            if any(name.startswith(prefix) for prefix in CORRELATION_ID_CONST_PREFIXES)
            and isinstance(getattr(helpers, name, None), str)
        ],
    )
    def test_individual_correlation_id_constant_format(self, const_name):
        """逐个验证每个 correlation_id 常量格式（参数化测试，便于定位问题）"""
        value = getattr(helpers, const_name)
        assert is_valid_correlation_id(value), (
            f"{const_name} = {value!r} 不符合 correlation_id 格式。\n"
            f"期望格式: ^corr-[a-fA-F0-9]{{16}}$ (corr- + 16位十六进制)\n"
            f"实际: corr- + {len(value) - 5}位"
        )

    def test_make_test_correlation_id_generates_valid_format(self):
        """make_test_correlation_id 生成的值必须符合格式"""
        from tests.gateway.helpers import make_test_correlation_id

        # 测试多个索引值
        for index in [0, 1, 42, 255, 65535, 0xFFFFFFFF]:
            corr_id = make_test_correlation_id(index)
            assert is_valid_correlation_id(corr_id), (
                f"make_test_correlation_id({index}) = {corr_id!r} 不符合格式"
            )

    def test_generate_compliant_correlation_id_is_valid(self):
        """generate_compliant_correlation_id 生成的值必须符合格式"""
        from tests.gateway.helpers import generate_compliant_correlation_id

        # 多次生成验证
        for _ in range(10):
            corr_id = generate_compliant_correlation_id()
            assert is_valid_correlation_id(corr_id), (
                f"generate_compliant_correlation_id() = {corr_id!r} 不符合格式"
            )

    def test_correlation_id_pattern_matches_schema_definition(self):
        """验证 CORRELATION_ID_PATTERN 与预期一致"""
        # 合规示例
        valid_examples = [
            "corr-0000000000000000",
            "corr-a1b2c3d4e5f67890",
            "corr-ABCDEF1234567890",
            "corr-ffffffffffffffff",
        ]
        for example in valid_examples:
            assert CORRELATION_ID_PATTERN.match(example), f"{example} 应该合规"

        # 不合规示例
        invalid_examples = [
            "corr-abc",  # 太短
            "corr-00000000000000000",  # 太长 (17位)
            "corr-000000000000000",  # 太短 (15位)
            "test-0000000000000000",  # 前缀错误
            "corr-ghij0000000000000",  # 包含非十六进制字符
            "CORR-0000000000000000",  # 前缀大小写错误
        ]
        for example in invalid_examples:
            assert not CORRELATION_ID_PATTERN.match(example), f"{example} 应该不合规"
