#!/usr/bin/env python3
"""
兼容入口：check_mypy_gate

向后兼容旧导入路径：
    from check_mypy_gate import main
"""

import importlib
import sys

_impl = importlib.import_module("scripts.ci.check_mypy_gate")
sys.modules[__name__] = _impl
