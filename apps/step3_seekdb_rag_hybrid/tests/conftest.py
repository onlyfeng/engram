"""
conftest.py - pytest 配置

配置测试所需的模块路径。
"""

import sys
from pathlib import Path

# 添加 step3 目录到路径
_step3_path = Path(__file__).parent.parent
if str(_step3_path) not in sys.path:
    sys.path.insert(0, str(_step3_path))

# 添加 apps 目录到路径（用于 step3_seekdb_rag_hybrid.xxx 形式的导入）
_apps_path = Path(__file__).parent.parent.parent
if str(_apps_path) not in sys.path:
    sys.path.insert(0, str(_apps_path))
