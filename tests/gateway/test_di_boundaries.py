"""
Gateway DI 边界测试

验证以下目录中不包含禁止的 import/调用模式：
- src/engram/gateway/handlers/**/*.py
- src/engram/gateway/services/**/*.py

确保 handlers 和 services 模块遵循依赖注入原则。

禁止的模式:
- get_container( : handlers/services 应通过 deps 获取依赖
- get_config(    : handlers/services 应通过 deps.config 获取配置
- get_client(    : handlers/services 应通过 deps.openmemory_client 获取客户端
- logbook_adapter.get_adapter( : handlers/services 应通过 deps.logbook_adapter 获取适配器
- GatewayDeps.create( : handlers/services 不应直接创建依赖容器
- deps is None   : handlers/services 不应检查 deps 是否为 None（依赖必须由调用方提供）

例外:
- 类型注释 (TYPE_CHECKING 块) 中的 import 不计入
- 带有 `# DI-BOUNDARY-ALLOW:` 标记的行（legacy fallback 兼容期）

此测试与 scripts/ci/check_gateway_di_boundaries.py 使用相同的检测逻辑，
可作为 pytest 的一部分运行，确保 PR 一旦引入违规调用即失败。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Set

import pytest

# ============================================================================
# 配置区（与 scripts/ci/check_gateway_di_boundaries.py 保持一致）
# ============================================================================

# 禁止的调用模式（正则表达式）
FORBIDDEN_PATTERNS: List[tuple[str, str]] = [
    (r"\bget_container\s*\(", "get_container("),
    (r"\bget_config\s*\(", "get_config("),
    (r"\bget_client\s*\(", "get_client("),
    (r"\blogbook_adapter\.get_adapter\s*\(", "logbook_adapter.get_adapter("),
    (r"\bGatewayDeps\.create\s*\(", "GatewayDeps.create("),
    (r"\bdeps\s+is\s+None\b", "deps is None"),
]

# 允许例外的文件（相对于 handlers 目录）
# 当前无文件级例外
ALLOWED_EXCEPTIONS: dict[str, Set[str]] = {}

# DI 边界允许标记（用于标识 legacy fallback 兼容分支）
# 格式: # DI-BOUNDARY-ALLOW: <reason>
DI_BOUNDARY_ALLOW_MARKER = "# DI-BOUNDARY-ALLOW:"


# ============================================================================
# 辅助函数
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录"""
    # 从测试文件位置向上查找项目根
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists() or (parent / "setup.py").exists():
            return parent
        if (parent / "src" / "engram").exists():
            return parent
    # 回退到假设的目录结构
    return current.parent.parent.parent


def is_in_type_checking_block(lines: List[str], line_index: int) -> bool:
    """检查指定行是否在 TYPE_CHECKING 块内"""
    in_block = False
    indent_level = -1

    for i in range(line_index - 1, -1, -1):
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        if re.match(r"^if\s+(typing\.)?TYPE_CHECKING\s*:", stripped):
            indent_level = len(line) - len(line.lstrip())
            in_block = True
            break

        current_indent = len(line) - len(line.lstrip())
        if current_indent == 0 and not stripped.startswith("if"):
            break

    if not in_block:
        return False

    current_line = lines[line_index]
    current_indent = len(current_line) - len(current_line.lstrip())
    return current_indent > indent_level


def is_in_docstring(lines: List[str], line_index: int) -> bool:
    """检查指定行是否在 docstring 中"""
    TRIPLE_DOUBLE = '"""'
    TRIPLE_SINGLE = "'''"

    in_docstring = False
    docstring_char = None

    for i in range(line_index):
        line = lines[i]
        for delim in [TRIPLE_DOUBLE, TRIPLE_SINGLE]:
            count = line.count(delim)
            if count > 0:
                if not in_docstring:
                    in_docstring = True
                    docstring_char = delim
                    if count % 2 == 0:
                        in_docstring = False
                        docstring_char = None
                elif delim == docstring_char:
                    in_docstring = False
                    docstring_char = None

    current_line = lines[line_index]
    if in_docstring:
        if docstring_char and docstring_char in current_line:
            return True
        return True

    for delim in [TRIPLE_DOUBLE, TRIPLE_SINGLE]:
        if delim in current_line:
            idx = current_line.find(delim)
            rest = current_line[idx + 3 :]
            if delim in rest:
                return True

    return False


def is_in_string_literal(line: str, match_start: int) -> bool:
    """检查匹配位置是否在字符串字面量内"""
    prefix = line[:match_start].replace('\\"', "").replace("\\'", "")
    double_quotes = prefix.count('"')
    single_quotes = prefix.count("'")
    return (double_quotes % 2 == 1) or (single_quotes % 2 == 1)


def has_di_boundary_allow_marker(lines: List[str], line_index: int) -> bool:
    """
    检查指定行是否有 DI-BOUNDARY-ALLOW 标记

    允许标记出现在：
    1. 当前行本身（行尾注释或上方注释）
    2. 上一行（注释行标记下一行代码）

    Args:
        lines: 文件所有行
        line_index: 当前行索引（0-based）

    Returns:
        True 如果该行被 DI-BOUNDARY-ALLOW 标记允许
    """
    current_line = lines[line_index]

    # 检查当前行是否包含标记（行尾注释）
    if DI_BOUNDARY_ALLOW_MARKER in current_line:
        return True

    # 检查上一行是否是 DI-BOUNDARY-ALLOW 注释行
    if line_index > 0:
        prev_line = lines[line_index - 1].strip()
        if prev_line.startswith(DI_BOUNDARY_ALLOW_MARKER):
            return True

    return False


def scan_file_for_violations(file_path: Path) -> List[tuple[int, str, str]]:
    """
    扫描单个文件中的违规调用

    Returns:
        List of (line_number, pattern_name, line_content) tuples
    """
    violations: List[tuple[int, str, str]] = []

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return violations

    lines = content.splitlines()
    file_name = file_path.name
    allowed_patterns = ALLOWED_EXCEPTIONS.get(file_name, set())

    for line_index, line in enumerate(lines):
        line_number = line_index + 1
        stripped = line.strip()

        if stripped.startswith("#"):
            continue

        if is_in_type_checking_block(lines, line_index):
            continue

        # 跳过 docstring 中的内容
        if is_in_docstring(lines, line_index):
            continue

        # 跳过带有 DI-BOUNDARY-ALLOW 标记的行
        if has_di_boundary_allow_marker(lines, line_index):
            continue

        for pattern_regex, pattern_name in FORBIDDEN_PATTERNS:
            if pattern_name in allowed_patterns:
                continue

            match = re.search(pattern_regex, line)
            if match:
                # 检查匹配是否在字符串字面量内
                if is_in_string_literal(line, match.start()):
                    continue

                violations.append((line_number, pattern_name, line.strip()))

    return violations


# ============================================================================
# 测试用例
# ============================================================================


class TestGatewayDIBoundaries:
    """Gateway DI 边界测试套件"""

    @pytest.fixture(scope="class")
    def handlers_dir(self) -> Path:
        """获取 handlers 目录路径"""
        project_root = get_project_root()
        return project_root / "src" / "engram" / "gateway" / "handlers"

    @pytest.fixture(scope="class")
    def services_dir(self) -> Path:
        """获取 services 目录路径"""
        project_root = get_project_root()
        return project_root / "src" / "engram" / "gateway" / "services"

    @pytest.fixture(scope="class")
    def handler_files(self, handlers_dir: Path) -> List[Path]:
        """获取所有 handler Python 文件"""
        if not handlers_dir.exists():
            pytest.skip(f"handlers 目录不存在: {handlers_dir}")
        return list(handlers_dir.rglob("*.py"))

    @pytest.fixture(scope="class")
    def service_files(self, services_dir: Path) -> List[Path]:
        """获取所有 service Python 文件"""
        if not services_dir.exists():
            pytest.skip(f"services 目录不存在: {services_dir}")
        return list(services_dir.rglob("*.py"))

    @pytest.fixture(scope="class")
    def all_boundary_files(
        self, handler_files: List[Path], service_files: List[Path]
    ) -> List[Path]:
        """获取所有需要检查 DI 边界的文件（handlers + services）"""
        return handler_files + service_files

    def test_handlers_dir_exists(self, handlers_dir: Path) -> None:
        """验证 handlers 目录存在"""
        assert handlers_dir.exists(), f"handlers 目录不存在: {handlers_dir}"

    def test_services_dir_exists(self, services_dir: Path) -> None:
        """验证 services 目录存在"""
        assert services_dir.exists(), f"services 目录不存在: {services_dir}"

    def test_no_forbidden_calls_in_handlers(self, handler_files: List[Path]) -> None:
        """
        验证 handlers 中无禁止的 DI 边界违规调用

        此测试扫描所有 handler 文件，检查是否存在:
        - get_container()
        - get_config()
        - get_client()
        - logbook_adapter.get_adapter()

        如发现违规，测试失败并报告具体位置。
        """
        all_violations: List[tuple[Path, int, str, str]] = []

        for file_path in handler_files:
            violations = scan_file_for_violations(file_path)
            for line_number, pattern, line_content in violations:
                all_violations.append((file_path, line_number, pattern, line_content))

        if all_violations:
            # 构建详细的错误消息
            error_lines = [f"\n发现 {len(all_violations)} 处 DI 边界违规调用:\n"]
            for file_path, line_number, pattern, line_content in all_violations:
                relative_path = file_path.relative_to(get_project_root())
                error_lines.append(f"  {relative_path}:{line_number}")
                error_lines.append(f"    禁止模式: {pattern}")
                error_lines.append(f"    代码: {line_content}")
                error_lines.append("")

            error_lines.append(
                "handlers 应通过 deps 参数获取依赖，不应直接调用全局获取函数。\n"
                "参见 docs/gateway/06_gateway_design.md"
            )

            pytest.fail("\n".join(error_lines))

    def test_no_forbidden_calls_in_services(self, service_files: List[Path]) -> None:
        """
        验证 services 中无禁止的 DI 边界违规调用

        此测试扫描所有 service 文件，检查是否存在:
        - get_container()
        - get_config()
        - get_client()
        - logbook_adapter.get_adapter()
        - GatewayDeps.create()
        - deps is None

        如发现违规，测试失败并报告具体位置。
        """
        all_violations: List[tuple[Path, int, str, str]] = []

        for file_path in service_files:
            violations = scan_file_for_violations(file_path)
            for line_number, pattern, line_content in violations:
                all_violations.append((file_path, line_number, pattern, line_content))

        if all_violations:
            # 构建详细的错误消息
            error_lines = [f"\n发现 {len(all_violations)} 处 DI 边界违规调用:\n"]
            for file_path, line_number, pattern, line_content in all_violations:
                relative_path = file_path.relative_to(get_project_root())
                error_lines.append(f"  {relative_path}:{line_number}")
                error_lines.append(f"    禁止模式: {pattern}")
                error_lines.append(f"    代码: {line_content}")
                error_lines.append("")

            error_lines.append(
                "services 应通过 deps 参数获取依赖，不应直接调用全局获取函数。\n"
                "参见 docs/gateway/06_gateway_design.md"
            )

            pytest.fail("\n".join(error_lines))

    @pytest.mark.parametrize(
        "pattern_name,pattern_regex",
        [
            ("get_container(", r"\bget_container\s*\("),
            ("get_config(", r"\bget_config\s*\("),
            ("get_client(", r"\bget_client\s*\("),
            ("logbook_adapter.get_adapter(", r"\blogbook_adapter\.get_adapter\s*\("),
            ("GatewayDeps.create(", r"\bGatewayDeps\.create\s*\("),
            ("deps is None", r"\bdeps\s+is\s+None\b"),
        ],
    )
    def test_individual_pattern_not_in_handlers(
        self,
        handler_files: List[Path],
        pattern_name: str,
        pattern_regex: str,
    ) -> None:
        """
        分别测试每个禁止模式（handlers）

        这提供了更细粒度的测试反馈，便于定位具体问题。
        """
        violations_for_pattern: List[tuple[Path, int, str]] = []

        for file_path in handler_files:
            file_name = file_path.name
            allowed = ALLOWED_EXCEPTIONS.get(file_name, set())

            if pattern_name in allowed:
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            lines = content.splitlines()

            for line_index, line in enumerate(lines):
                line_number = line_index + 1
                stripped = line.strip()

                if stripped.startswith("#"):
                    continue

                if is_in_type_checking_block(lines, line_index):
                    continue

                # 跳过 docstring 中的内容
                if is_in_docstring(lines, line_index):
                    continue

                # 跳过带有 DI-BOUNDARY-ALLOW 标记的行
                if has_di_boundary_allow_marker(lines, line_index):
                    continue

                match = re.search(pattern_regex, line)
                if match:
                    # 检查匹配是否在字符串字面量内
                    if is_in_string_literal(line, match.start()):
                        continue

                    violations_for_pattern.append((file_path, line_number, line.strip()))

        if violations_for_pattern:
            error_lines = [f"\n发现 {len(violations_for_pattern)} 处 '{pattern_name}' 违规调用:\n"]
            for file_path, line_number, line_content in violations_for_pattern:
                relative_path = file_path.relative_to(get_project_root())
                error_lines.append(f"  {relative_path}:{line_number}: {line_content}")

            pytest.fail("\n".join(error_lines))

    @pytest.mark.parametrize(
        "pattern_name,pattern_regex",
        [
            ("get_container(", r"\bget_container\s*\("),
            ("get_config(", r"\bget_config\s*\("),
            ("get_client(", r"\bget_client\s*\("),
            ("logbook_adapter.get_adapter(", r"\blogbook_adapter\.get_adapter\s*\("),
            ("GatewayDeps.create(", r"\bGatewayDeps\.create\s*\("),
            ("deps is None", r"\bdeps\s+is\s+None\b"),
        ],
    )
    def test_individual_pattern_not_in_services(
        self,
        service_files: List[Path],
        pattern_name: str,
        pattern_regex: str,
    ) -> None:
        """
        分别测试每个禁止模式（services）

        这提供了更细粒度的测试反馈，便于定位具体问题。
        """
        violations_for_pattern: List[tuple[Path, int, str]] = []

        for file_path in service_files:
            file_name = file_path.name
            allowed = ALLOWED_EXCEPTIONS.get(file_name, set())

            if pattern_name in allowed:
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            lines = content.splitlines()

            for line_index, line in enumerate(lines):
                line_number = line_index + 1
                stripped = line.strip()

                if stripped.startswith("#"):
                    continue

                if is_in_type_checking_block(lines, line_index):
                    continue

                # 跳过 docstring 中的内容
                if is_in_docstring(lines, line_index):
                    continue

                # 跳过带有 DI-BOUNDARY-ALLOW 标记的行
                if has_di_boundary_allow_marker(lines, line_index):
                    continue

                match = re.search(pattern_regex, line)
                if match:
                    # 检查匹配是否在字符串字面量内
                    if is_in_string_literal(line, match.start()):
                        continue

                    violations_for_pattern.append((file_path, line_number, line.strip()))

        if violations_for_pattern:
            error_lines = [f"\n发现 {len(violations_for_pattern)} 处 '{pattern_name}' 违规调用:\n"]
            for file_path, line_number, line_content in violations_for_pattern:
                relative_path = file_path.relative_to(get_project_root())
                error_lines.append(f"  {relative_path}:{line_number}: {line_content}")

            pytest.fail("\n".join(error_lines))


class TestDIBoundaryExceptions:
    """测试 DI 边界例外配置"""

    def test_no_file_level_exceptions(self) -> None:
        """验证当前无文件级例外"""
        assert len(ALLOWED_EXCEPTIONS) == 0, "不应有文件级例外"

    def test_forbidden_patterns_defined(self) -> None:
        """验证禁止模式列表非空"""
        assert len(FORBIDDEN_PATTERNS) > 0

    def test_new_patterns_included(self) -> None:
        """验证新增的禁止模式已包含"""
        pattern_names = [name for _, name in FORBIDDEN_PATTERNS]
        assert "GatewayDeps.create(" in pattern_names
        assert "deps is None" in pattern_names

    def test_all_patterns_have_names(self) -> None:
        """验证所有禁止模式都有描述性名称"""
        for pattern_regex, pattern_name in FORBIDDEN_PATTERNS:
            assert pattern_name, f"模式 {pattern_regex} 缺少名称"

    def test_di_boundary_allow_marker_defined(self) -> None:
        """验证 DI-BOUNDARY-ALLOW 标记已定义"""
        assert DI_BOUNDARY_ALLOW_MARKER == "# DI-BOUNDARY-ALLOW:"


class TestDIBoundaryAllowMarker:
    """测试 DI-BOUNDARY-ALLOW 标记功能"""

    def test_marker_on_same_line(self) -> None:
        """测试同行标记（行尾注释）"""
        lines = [
            "def foo():",
            "    config = get_config()  # DI-BOUNDARY-ALLOW: legacy fallback",
            "    return config",
        ]
        # 第 1 行（index=1）应该被允许
        assert has_di_boundary_allow_marker(lines, 1) is True
        # 第 2 行不应该被允许
        assert has_di_boundary_allow_marker(lines, 2) is False

    def test_marker_on_previous_line(self) -> None:
        """测试上一行标记（注释行标记下一行代码）"""
        lines = [
            "def foo():",
            "    # DI-BOUNDARY-ALLOW: legacy fallback",
            "    config = get_config()",
            "    return config",
        ]
        # 第 2 行（index=2）应该被允许（因为上一行有标记）
        assert has_di_boundary_allow_marker(lines, 2) is True
        # 第 3 行不应该被允许
        assert has_di_boundary_allow_marker(lines, 3) is False

    def test_no_marker(self) -> None:
        """测试没有标记的行"""
        lines = [
            "def foo():",
            "    config = get_config()",
            "    return config",
        ]
        assert has_di_boundary_allow_marker(lines, 1) is False
        assert has_di_boundary_allow_marker(lines, 2) is False

    def test_marker_with_reason(self) -> None:
        """测试带原因的标记"""
        lines = [
            "# DI-BOUNDARY-ALLOW: v0.9 兼容期，v1.0 移除",
            "deps = GatewayDeps.create(config=config)",
        ]
        assert has_di_boundary_allow_marker(lines, 1) is True


# ============================================================================
# deps 注入验证测试：验证当 deps 已提供时不会触发全局获取函数
# ============================================================================


class TestDepsInjectionNoGlobalFallback:
    """
    验证当 deps 已提供时，handlers 不会触发全局获取函数

    此测试类确保 DI 边界的运行时正确性：
    - memory_store_impl 使用 deps.config 而非 get_config()
    - memory_query_impl 使用 deps.openmemory_client 而非 get_client()
    - governance_update_impl 使用 deps.db 而非 get_db()

    测试方法：
    - mock 全局获取函数
    - 通过 deps 注入 fake 依赖
    - 执行 handler
    - 验证全局获取函数未被调用
    """

    @pytest.mark.asyncio
    async def test_memory_store_uses_deps_config_not_global(
        self, fake_gateway_config, fake_logbook_db, fake_openmemory_client, fake_logbook_adapter
    ) -> None:
        """
        验证 memory_store_impl 使用 deps.config 而非 get_config()

        当 deps 已提供时，handler 应从 deps.config 获取配置，
        不应调用 get_config() 全局函数。
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl

        # 创建 mock get_config
        mock_get_config = MagicMock()

        # 配置 fake_logbook_db 允许写入
        fake_logbook_db.configure_settings(team_write_enabled=True, policy_json={})

        # 配置 fake_openmemory_client 返回成功
        fake_openmemory_client.configure_store_success(memory_id="test_mem_123")

        # 配置 fake_logbook_adapter
        fake_logbook_adapter.configure_dedup_miss()

        # 创建 deps，显式注入所有依赖（包括 logbook_adapter）
        deps = GatewayDeps.for_testing(
            config=fake_gateway_config,
            db=fake_logbook_db,
            logbook_adapter=fake_logbook_adapter,
            openmemory_client=fake_openmemory_client,
        )

        # patch get_config（如果被调用将抛出异常或记录调用）
        with patch("engram.gateway.config.get_config", mock_get_config):
            result = await memory_store_impl(
                payload_md="test content",
                correlation_id="corr-0000000000000001",
                deps=deps,
            )

        # 验证 get_config 未被调用
        mock_get_config.assert_not_called()

        # 验证 handler 正常工作
        assert result.ok is True
        assert result.memory_id == "test_mem_123"

    @pytest.mark.asyncio
    async def test_memory_query_uses_deps_client_not_global(
        self, fake_gateway_config, fake_openmemory_client
    ) -> None:
        """
        验证 memory_query_impl 使用 deps.openmemory_client 而非 get_client()

        当 deps 已提供时，handler 应从 deps.openmemory_client 获取客户端，
        不应调用 get_client() 全局函数。
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_query import memory_query_impl

        # 创建 mock get_client
        mock_get_client = MagicMock()

        # 配置 fake_openmemory_client 返回成功
        fake_openmemory_client.configure_search_success(
            results=[{"id": "mem_1", "content": "test"}]
        )

        # 创建 deps
        deps = GatewayDeps.for_testing(
            config=fake_gateway_config,
            openmemory_client=fake_openmemory_client,
        )

        # patch get_client
        with patch("engram.gateway.openmemory_client.get_client", mock_get_client):
            result = await memory_query_impl(
                query="test query",
                correlation_id="corr-0000000000000002",
                deps=deps,
            )

        # 验证 get_client 未被调用
        mock_get_client.assert_not_called()

        # 验证 handler 正常工作
        assert result.ok is True
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_governance_update_uses_deps_db_not_global(
        self, fake_gateway_config, fake_logbook_db
    ) -> None:
        """
        验证 governance_update_impl 使用 deps.db 而非 get_db()

        当 deps 已提供时，handler 应从 deps.db 获取数据库实例，
        不应调用 get_db() 全局函数。
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.governance_update import governance_update_impl

        # 创建 mock get_db
        mock_get_db = MagicMock()

        # 配置 fake_gateway_config 的 admin_key
        fake_gateway_config.governance_admin_key = "test_admin_key"

        # 配置 fake_logbook_db
        fake_logbook_db.configure_settings(team_write_enabled=False, policy_json={})

        # 创建 fake_logbook_adapter
        from tests.gateway.fakes import FakeLogbookAdapter

        fake_adapter = FakeLogbookAdapter()

        # 创建 deps
        deps = GatewayDeps.for_testing(
            config=fake_gateway_config,
            db=fake_logbook_db,
            logbook_adapter=fake_adapter,
        )

        # patch get_db
        with patch("engram.gateway.logbook_db.get_db", mock_get_db):
            result = await governance_update_impl(
                team_write_enabled=True,
                admin_key="test_admin_key",
                deps=deps,
            )

        # 验证 get_db 未被调用
        mock_get_db.assert_not_called()

        # 验证 handler 正常工作（鉴权通过）
        assert result.ok is True
        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_all_handlers_use_deps_only(
        self,
        fake_gateway_config,
        fake_logbook_db,
        fake_openmemory_client,
        fake_logbook_adapter,
    ) -> None:
        """
        综合测试：所有 handler 调用时，全局获取函数均未被调用

        同时 mock 所有全局获取函数，验证完整的 DI 隔离。
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.governance_update import governance_update_impl
        from engram.gateway.handlers.memory_query import memory_query_impl
        from engram.gateway.handlers.memory_store import memory_store_impl

        # 创建所有 mock
        mock_get_config = MagicMock()
        mock_get_client = MagicMock()
        mock_get_db = MagicMock()
        mock_get_adapter = MagicMock()

        # 配置 fakes
        fake_gateway_config.governance_admin_key = "admin_key"
        fake_logbook_db.configure_settings(team_write_enabled=True, policy_json={})
        fake_openmemory_client.configure_store_success(memory_id="mem_comprehensive")
        fake_openmemory_client.configure_search_success()

        # 创建 deps
        deps = GatewayDeps.for_testing(
            config=fake_gateway_config,
            db=fake_logbook_db,
            logbook_adapter=fake_logbook_adapter,
            openmemory_client=fake_openmemory_client,
        )

        # 一次性 patch 所有全局获取函数
        with (
            patch("engram.gateway.config.get_config", mock_get_config),
            patch("engram.gateway.openmemory_client.get_client", mock_get_client),
            patch("engram.gateway.logbook_db.get_db", mock_get_db),
            patch("engram.gateway.logbook_adapter.get_adapter", mock_get_adapter),
        ):
            # 测试 memory_store
            store_result = await memory_store_impl(
                payload_md="comprehensive test",
                correlation_id="corr-c0000000000000a1",
                deps=deps,
            )
            assert store_result.ok is True

            # 测试 memory_query
            query_result = await memory_query_impl(
                query="comprehensive query",
                correlation_id="corr-c0000000000000a2",
                deps=deps,
            )
            assert query_result.ok is True

            # 测试 governance_update
            gov_result = await governance_update_impl(
                team_write_enabled=True,
                admin_key="admin_key",
                deps=deps,
            )
            assert gov_result.ok is True

        # 验证所有全局获取函数均未被调用
        mock_get_config.assert_not_called()
        mock_get_client.assert_not_called()
        mock_get_db.assert_not_called()
        mock_get_adapter.assert_not_called()


# ============================================================================
# Container 无副作用检查测试：验证检查 container 是否存在不会触发 load_config
# ============================================================================


class TestCorrelationIdRequired:
    """
    验证 correlation_id 必需参数契约

    此测试类确保：
    1. correlation_id 是必需参数（keyword-only, str 类型）
    2. 传入 None 时抛出 ValueError 而非触发全局依赖获取
    3. 错误消息稳定，便于调用方处理
    """

    @pytest.mark.asyncio
    async def test_memory_query_missing_correlation_id_raises_typeerror(
        self, fake_gateway_config, fake_openmemory_client
    ) -> None:
        """
        验证 memory_query_impl 未传 correlation_id 时抛出 TypeError

        由于 correlation_id 是 keyword-only 必需参数，不传时 Python 会抛出 TypeError。
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_query import memory_query_impl

        deps = GatewayDeps.for_testing(
            config=fake_gateway_config,
            openmemory_client=fake_openmemory_client,
        )

        with pytest.raises(TypeError) as exc_info:
            await memory_query_impl(
                query="test query",
                deps=deps,
                # correlation_id 未传
            )

        # 验证错误消息包含 correlation_id
        assert "correlation_id" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_memory_query_none_correlation_id_raises_valueerror(
        self, fake_gateway_config, fake_openmemory_client
    ) -> None:
        """
        验证 memory_query_impl 传入 None 作为 correlation_id 时抛出 ValueError

        即使绕过类型检查传入 None，handler 的防御性检查也会阻断并抛出 ValueError。
        此测试确保错误类型和消息稳定。
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_query import memory_query_impl

        # Mock 全局获取函数，确保不会被调用
        mock_get_config = MagicMock()
        mock_get_client = MagicMock()

        deps = GatewayDeps.for_testing(
            config=fake_gateway_config,
            openmemory_client=fake_openmemory_client,
        )

        with (
            patch("engram.gateway.config.get_config", mock_get_config),
            patch("engram.gateway.openmemory_client.get_client", mock_get_client),
        ):
            with pytest.raises(ValueError) as exc_info:
                await memory_query_impl(
                    query="test query",
                    correlation_id=None,  # type: ignore[arg-type]
                    deps=deps,
                )

        # 验证错误消息稳定
        error_msg = str(exc_info.value)
        assert "correlation_id" in error_msg
        assert "必需参数" in error_msg or "HTTP 入口层" in error_msg

        # 验证全局获取函数未被调用
        mock_get_config.assert_not_called()
        mock_get_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_memory_store_missing_correlation_id_raises_typeerror(
        self, fake_gateway_config, fake_logbook_db, fake_openmemory_client, fake_logbook_adapter
    ) -> None:
        """
        验证 memory_store_impl 未传 correlation_id 时抛出 TypeError

        由于 correlation_id 是 keyword-only 必需参数，不传时 Python 会抛出 TypeError。
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl

        deps = GatewayDeps.for_testing(
            config=fake_gateway_config,
            db=fake_logbook_db,
            logbook_adapter=fake_logbook_adapter,
            openmemory_client=fake_openmemory_client,
        )

        with pytest.raises(TypeError) as exc_info:
            await memory_store_impl(
                payload_md="test content",
                deps=deps,
                # correlation_id 未传
            )

        # 验证错误消息包含 correlation_id
        assert "correlation_id" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_memory_store_none_correlation_id_raises_valueerror(
        self, fake_gateway_config, fake_logbook_db, fake_openmemory_client, fake_logbook_adapter
    ) -> None:
        """
        验证 memory_store_impl 传入 None 作为 correlation_id 时抛出 ValueError

        即使绕过类型检查传入 None，handler 的防御性检查也会阻断并抛出 ValueError。
        此测试确保错误类型和消息稳定，且不触发全局依赖获取。
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl

        # Mock 全局获取函数，确保不会被调用
        mock_get_config = MagicMock()
        mock_get_client = MagicMock()
        mock_get_db = MagicMock()

        deps = GatewayDeps.for_testing(
            config=fake_gateway_config,
            db=fake_logbook_db,
            logbook_adapter=fake_logbook_adapter,
            openmemory_client=fake_openmemory_client,
        )

        with (
            patch("engram.gateway.config.get_config", mock_get_config),
            patch("engram.gateway.openmemory_client.get_client", mock_get_client),
            patch("engram.gateway.logbook_db.get_db", mock_get_db),
        ):
            with pytest.raises(ValueError) as exc_info:
                await memory_store_impl(
                    payload_md="test content",
                    correlation_id=None,  # type: ignore[arg-type]
                    deps=deps,
                )

        # 验证错误消息稳定
        error_msg = str(exc_info.value)
        assert "correlation_id" in error_msg
        assert "必需参数" in error_msg or "HTTP 入口层" in error_msg

        # 验证全局获取函数未被调用（correlation_id 检查在依赖使用之前）
        mock_get_config.assert_not_called()
        mock_get_client.assert_not_called()
        mock_get_db.assert_not_called()

    @pytest.mark.asyncio
    async def test_correlation_id_error_message_is_stable(
        self, fake_gateway_config, fake_openmemory_client
    ) -> None:
        """
        验证 correlation_id 错误消息格式稳定

        错误消息应该包含：
        1. 明确指出 correlation_id 是必需参数
        2. 指导调用方正确的传参方式（HTTP 入口层生成）
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_query import memory_query_impl

        deps = GatewayDeps.for_testing(
            config=fake_gateway_config,
            openmemory_client=fake_openmemory_client,
        )

        with pytest.raises(ValueError) as exc_info:
            await memory_query_impl(
                query="test",
                correlation_id=None,  # type: ignore[arg-type]
                deps=deps,
            )

        error_msg = str(exc_info.value)

        # 验证错误消息的关键组成部分
        assert "correlation_id" in error_msg
        # 消息应该指导正确的使用方式
        assert "HTTP 入口层" in error_msg or "handler 不再自行生成" in error_msg


class TestContainerCheckWithoutEnvSideEffects:
    """
    无环境变量时检查 container 不触发 load_config 的测试

    此测试类验证 container.py 中的无副作用检查函数：
    - is_container_set(): 返回布尔值，检查容器是否已设置
    - get_container_or_none(): 返回容器或 None

    关键约束：
    - 这些函数不应触发 load_config()
    - 即使 PROJECT_KEY/POSTGRES_DSN 未设置也可安全调用
    - 用于 lifespan 早期检查、测试 setup 等场景
    """

    def test_is_container_set_does_not_trigger_load_config(self, monkeypatch) -> None:
        """
        验证 is_container_set() 不触发 load_config()

        即使 PROJECT_KEY/POSTGRES_DSN 未设置，
        调用 is_container_set() 也不应触发 ConfigError 或 load_config()。
        """
        from unittest.mock import MagicMock, patch

        # 清除环境变量
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        # 重置全局单例
        from engram.gateway.container import is_container_set, reset_all_singletons

        reset_all_singletons()

        # 创建 mock load_config
        mock_load_config = MagicMock()

        try:
            with patch("engram.gateway.config.load_config", mock_load_config):
                # 调用 is_container_set 不应触发 load_config
                result = is_container_set()

            # 验证 load_config 未被调用
            mock_load_config.assert_not_called()

            # 结果应该是 False（因为 container 未初始化）
            assert result is False
        finally:
            reset_all_singletons()

    def test_get_container_or_none_does_not_trigger_load_config(self, monkeypatch) -> None:
        """
        验证 get_container_or_none() 不触发 load_config()

        即使 PROJECT_KEY/POSTGRES_DSN 未设置，
        调用 get_container_or_none() 也不应触发 ConfigError 或 load_config()。
        """
        from unittest.mock import MagicMock, patch

        # 清除环境变量
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        # 重置全局单例
        from engram.gateway.container import get_container_or_none, reset_all_singletons

        reset_all_singletons()

        # 创建 mock load_config
        mock_load_config = MagicMock()

        try:
            with patch("engram.gateway.config.load_config", mock_load_config):
                # 调用 get_container_or_none 不应触发 load_config
                result = get_container_or_none()

            # 验证 load_config 未被调用
            mock_load_config.assert_not_called()

            # 结果应该是 None（因为 container 未初始化）
            assert result is None
        finally:
            reset_all_singletons()

    def test_is_container_set_returns_true_after_set_container(self, monkeypatch) -> None:
        """
        验证 set_container 后 is_container_set() 返回 True

        此测试验证 is_container_set() 正确反映容器状态。
        """
        # 清除环境变量（确保测试不依赖环境）
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        from engram.gateway.config import GatewayConfig
        from engram.gateway.container import (
            GatewayContainer,
            is_container_set,
            reset_all_singletons,
            set_container,
        )

        reset_all_singletons()

        try:
            # 初始状态应该是 False
            assert is_container_set() is False

            # 创建并设置 container
            config = GatewayConfig(
                project_key="test-project",
                postgres_dsn="postgresql://test@localhost/test",
                openmemory_base_url="http://localhost:8080",
            )
            container = GatewayContainer(config=config)
            set_container(container)

            # 现在应该返回 True
            assert is_container_set() is True
        finally:
            reset_all_singletons()

    def test_container_check_before_lifespan_no_config_error(self, monkeypatch) -> None:
        """
        模拟 lifespan 早期检查场景：无环境变量时检查 container 不抛异常

        此测试模拟 main.py::lifespan 中的检查逻辑：
        - 使用 is_container_set() 检查容器是否已注入
        - 不触发 ConfigError，即使环境变量未设置
        """
        # 清除环境变量
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        from engram.gateway.config import ConfigError
        from engram.gateway.container import is_container_set, reset_all_singletons

        reset_all_singletons()

        try:
            # 模拟 lifespan 中的检查逻辑
            # 这不应该抛出 ConfigError
            container_initialized = is_container_set()

            # 验证结果
            assert container_initialized is False
        except ConfigError:
            pytest.fail("is_container_set() 不应触发 ConfigError")
        finally:
            reset_all_singletons()

    def test_get_container_triggers_load_config_contrast(self, monkeypatch) -> None:
        """
        对比测试：get_container() 会触发 load_config()

        此测试验证 get_container() 与无副作用函数的区别：
        - get_container() 会触发 load_config()
        - 如果环境变量未设置，会抛出 ConfigError
        """
        # 清除环境变量
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        from engram.gateway.config import ConfigError
        from engram.gateway.container import get_container, reset_all_singletons

        reset_all_singletons()

        try:
            # get_container() 应该触发 ConfigError（因为环境变量未设置）
            with pytest.raises(ConfigError):
                get_container()
        finally:
            reset_all_singletons()


# ============================================================================
# Config 无副作用检查测试：验证检查 config 是否存在不会触发 load_config
# ============================================================================


class TestConfigCheckWithoutEnvSideEffects:
    """
    无环境变量时检查 config 不触发 load_config 的测试

    此测试类验证 config.py 中的无副作用检查函数：
    - get_config_or_none(): 返回配置或 None

    关键约束：
    - 这些函数不应触发 load_config()
    - 即使 PROJECT_KEY/POSTGRES_DSN 未设置也可安全调用
    - 用于 lifespan 早期检查、测试 setup 等场景
    """

    def test_get_config_or_none_does_not_trigger_load_config(self, monkeypatch) -> None:
        """
        验证 get_config_or_none() 不触发 load_config()

        即使 PROJECT_KEY/POSTGRES_DSN 未设置，
        调用 get_config_or_none() 也不应触发 ConfigError 或 load_config()。
        """
        from unittest.mock import MagicMock, patch

        # 清除环境变量
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        # 重置全局单例
        from engram.gateway.config import get_config_or_none, reset_config

        reset_config()

        # 创建 mock load_config
        mock_load_config = MagicMock()

        try:
            with patch("engram.gateway.config.load_config", mock_load_config):
                # 调用 get_config_or_none 不应触发 load_config
                result = get_config_or_none()

            # 验证 load_config 未被调用
            mock_load_config.assert_not_called()

            # 结果应该是 None（因为 config 未初始化）
            assert result is None
        finally:
            reset_config()

    def test_get_config_triggers_load_config_contrast(self, monkeypatch) -> None:
        """
        对比测试：get_config() 会触发 load_config()

        此测试验证 get_config() 与无副作用函数的区别：
        - get_config() 会触发 load_config()
        - 如果环境变量未设置，会抛出 ConfigError
        """
        # 清除环境变量
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        from engram.gateway.config import ConfigError, get_config, reset_config

        reset_config()

        try:
            # get_config() 应该触发 ConfigError（因为环境变量未设置）
            with pytest.raises(ConfigError):
                get_config()
        finally:
            reset_config()

    def test_config_check_sequence_safe_without_env(self, monkeypatch) -> None:
        """
        验证安全检查序列：先用 get_config_or_none() 检查，再决定是否调用 get_config()

        此测试模拟 main.py::lifespan 中的安全检查模式：
        1. 先用 get_config_or_none() 检查是否已注入
        2. 如果已注入，直接使用
        3. 如果未注入，再调用 get_config() 触发加载
        """
        # 清除环境变量
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        from engram.gateway.config import (
            ConfigError,
            get_config,
            get_config_or_none,
            reset_config,
        )

        reset_config()

        try:
            # 第一步：安全检查，不抛出异常
            config = get_config_or_none()
            assert config is None

            # 第二步：如果需要实际配置，才调用 get_config()（此时会抛出）
            with pytest.raises(ConfigError):
                get_config()
        finally:
            reset_config()


# ============================================================================
# OpenMemoryClient 无副作用检查测试：验证检查 client 是否存在不会触发构造
# ============================================================================


class TestOpenMemoryClientCheckWithoutEnvSideEffects:
    """
    无环境变量时检查 openmemory_client 不触发构造的测试

    此测试类验证 openmemory_client.py 中的无副作用检查函数：
    - get_client_or_none(): 返回客户端或 None

    关键约束：
    - 这些函数不应触发 OpenMemoryClient 构造
    - 即使 OPENMEMORY_BASE_URL 未设置也可安全调用
    - 用于检查全局单例状态
    """

    def test_get_client_or_none_does_not_trigger_construction(self, monkeypatch) -> None:
        """
        验证 get_client_or_none() 不触发 OpenMemoryClient 构造

        即使环境变量未设置，
        调用 get_client_or_none() 也不应触发客户端构造。
        """
        from unittest.mock import MagicMock, patch

        # 清除环境变量
        monkeypatch.delenv("OPENMEMORY_BASE_URL", raising=False)
        monkeypatch.delenv("OPENMEMORY_API_KEY", raising=False)
        monkeypatch.delenv("OM_API_KEY", raising=False)

        # 重置全局单例
        from engram.gateway.openmemory_client import get_client_or_none, reset_client

        reset_client()

        # 创建 mock OpenMemoryClient.__init__
        mock_init = MagicMock(return_value=None)

        try:
            with patch(
                "engram.gateway.openmemory_client.OpenMemoryClient.__init__", mock_init
            ):
                # 调用 get_client_or_none 不应触发构造
                result = get_client_or_none()

            # 验证 __init__ 未被调用
            mock_init.assert_not_called()

            # 结果应该是 None（因为 client 未初始化）
            assert result is None
        finally:
            reset_client()

    def test_get_client_triggers_construction_contrast(self, monkeypatch) -> None:
        """
        对比测试：get_client() 会触发 OpenMemoryClient 构造

        此测试验证 get_client() 与无副作用函数的区别：
        - get_client() 会触发 OpenMemoryClient 构造
        - 使用默认环境变量值构造客户端
        """
        # 清除环境变量（使用默认值）
        monkeypatch.delenv("OPENMEMORY_BASE_URL", raising=False)

        # 重置全局单例
        from engram.gateway.openmemory_client import get_client, reset_client

        reset_client()

        try:
            # get_client() 应该触发构造（使用默认值）
            client = get_client()

            # 验证客户端已构造
            assert client is not None
            # 默认 base_url 是 http://127.0.0.1:8080
            assert "127.0.0.1" in client.base_url or "localhost" in client.base_url
        finally:
            reset_client()

    def test_client_check_sequence_safe_without_http(self, monkeypatch) -> None:
        """
        验证安全检查序列不触发真实 HTTP

        此测试确保：
        1. get_client_or_none() 不触发 HTTP
        2. 仅构造 OpenMemoryClient 不触发 HTTP（只有 add_memory/search 等方法才会）
        """
        from unittest.mock import MagicMock, patch

        # 清除环境变量
        monkeypatch.delenv("OPENMEMORY_BASE_URL", raising=False)

        from engram.gateway.openmemory_client import get_client_or_none, reset_client

        reset_client()

        # Mock httpx.Client 确保不会发起真实请求
        mock_httpx_client = MagicMock()

        try:
            with patch("httpx.Client", mock_httpx_client):
                # 检查全局单例状态 - 不应触发任何 HTTP
                result = get_client_or_none()
                assert result is None

            # 验证 httpx.Client 未被调用
            mock_httpx_client.assert_not_called()
        finally:
            reset_client()


# ============================================================================
# LogbookAdapter 无副作用检查测试：验证检查 adapter 是否存在不会触发构造
# ============================================================================


class TestLogbookAdapterCheckWithoutEnvSideEffects:
    """
    无环境变量时检查 logbook_adapter 不触发构造的测试

    此测试类验证 logbook_adapter.py 中的无副作用检查函数：
    - get_adapter_or_none(): 返回适配器或 None

    关键约束：
    - 这些函数不应触发 LogbookAdapter 构造
    - 即使 POSTGRES_DSN 未设置也可安全调用
    - 用于检查全局单例状态
    """

    def test_get_adapter_or_none_does_not_trigger_construction(self, monkeypatch) -> None:
        """
        验证 get_adapter_or_none() 不触发 LogbookAdapter 构造

        即使 POSTGRES_DSN 未设置，
        调用 get_adapter_or_none() 也不应触发适配器构造。
        """
        from unittest.mock import MagicMock, patch

        # 清除环境变量
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        monkeypatch.delenv("TEST_PG_DSN", raising=False)

        # 重置全局单例
        from engram.gateway.logbook_adapter import get_adapter_or_none, reset_adapter

        reset_adapter()

        # 创建 mock LogbookAdapter.__init__
        mock_init = MagicMock(return_value=None)

        try:
            with patch("engram.gateway.logbook_adapter.LogbookAdapter.__init__", mock_init):
                # 调用 get_adapter_or_none 不应触发构造
                result = get_adapter_or_none()

            # 验证 __init__ 未被调用
            mock_init.assert_not_called()

            # 结果应该是 None（因为 adapter 未初始化）
            assert result is None
        finally:
            reset_adapter()

    def test_get_adapter_triggers_construction_contrast(self, monkeypatch) -> None:
        """
        对比测试：get_adapter() 会触发 LogbookAdapter 构造

        此测试验证 get_adapter() 与无副作用函数的区别：
        - get_adapter() 会触发 LogbookAdapter 构造
        - 构造时会设置 POSTGRES_DSN 环境变量
        """
        # 清除环境变量
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        # 重置全局单例
        from engram.gateway.logbook_adapter import get_adapter, reset_adapter

        reset_adapter()

        try:
            # get_adapter() 应该触发构造
            # 注意：LogbookAdapter 构造时不会立即连接数据库，只是保存 DSN
            adapter = get_adapter()

            # 验证适配器已构造
            assert adapter is not None
        finally:
            reset_adapter()

    def test_adapter_check_sequence_safe_without_db_connection(self, monkeypatch) -> None:
        """
        验证安全检查序列不触发数据库连接

        此测试确保：
        1. get_adapter_or_none() 不触发数据库连接
        2. 仅构造 LogbookAdapter 不触发数据库连接
        """
        from unittest.mock import MagicMock, patch

        # 清除环境变量
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        from engram.gateway.logbook_adapter import get_adapter_or_none, reset_adapter

        reset_adapter()

        # Mock get_connection 确保不会发起真实数据库连接
        mock_get_connection = MagicMock()

        try:
            with patch("engram.gateway.logbook_adapter.get_connection", mock_get_connection):
                # 检查全局单例状态 - 不应触发任何数据库连接
                result = get_adapter_or_none()
                assert result is None

            # 验证 get_connection 未被调用
            mock_get_connection.assert_not_called()
        finally:
            reset_adapter()


# ============================================================================
# 综合无副作用测试：验证所有模块的检查函数可以安全组合使用
# ============================================================================


class TestAllModulesNoSideEffectsOnCheck:
    """
    综合测试：验证所有模块的检查函数可以安全组合使用

    此测试类模拟真实的 lifespan 早期检查场景：
    - 检查所有单例是否已注入
    - 不触发任何配置加载、HTTP 请求或数据库连接
    """

    def test_all_checks_safe_without_env(self, monkeypatch) -> None:
        """
        验证所有检查函数在无环境变量时都安全

        此测试同时调用所有模块的检查函数，
        确保它们可以安全组合使用，不会触发副作用。
        """
        from unittest.mock import MagicMock, patch

        # 清除所有相关环境变量
        for key in [
            "PROJECT_KEY",
            "POSTGRES_DSN",
            "TEST_PG_DSN",
            "OPENMEMORY_BASE_URL",
            "OPENMEMORY_API_KEY",
            "OM_API_KEY",
        ]:
            monkeypatch.delenv(key, raising=False)

        # 重置所有全局单例
        from engram.gateway.config import get_config_or_none, reset_config
        from engram.gateway.container import (
            get_container_or_none,
            is_container_set,
            reset_all_singletons,
        )
        from engram.gateway.logbook_adapter import get_adapter_or_none, reset_adapter
        from engram.gateway.openmemory_client import get_client_or_none, reset_client

        reset_all_singletons()
        reset_config()
        reset_adapter()
        reset_client()

        # 创建所有相关的 mock
        mock_load_config = MagicMock()
        mock_httpx_client = MagicMock()
        mock_get_connection = MagicMock()

        try:
            with (
                patch("engram.gateway.config.load_config", mock_load_config),
                patch("httpx.Client", mock_httpx_client),
                patch("engram.gateway.logbook_adapter.get_connection", mock_get_connection),
            ):
                # 执行所有检查函数
                container_set = is_container_set()
                container = get_container_or_none()
                config = get_config_or_none()
                client = get_client_or_none()
                adapter = get_adapter_or_none()

                # 验证所有结果都是 None/False
                assert container_set is False
                assert container is None
                assert config is None
                assert client is None
                assert adapter is None

            # 验证没有触发任何副作用
            mock_load_config.assert_not_called()
            mock_httpx_client.assert_not_called()
            mock_get_connection.assert_not_called()
        finally:
            reset_all_singletons()
            reset_config()
            reset_adapter()
            reset_client()

    def test_lifespan_early_check_pattern(self, monkeypatch) -> None:
        """
        验证 lifespan 早期检查模式的正确用法

        此测试模拟 main.py::lifespan 中的检查逻辑：
        1. 先检查是否已注入（测试场景）
        2. 如果未注入，再执行初始化（生产场景）
        """
        from unittest.mock import MagicMock, patch

        # 清除环境变量
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        from engram.gateway.config import ConfigError
        from engram.gateway.container import (
            get_container,
            is_container_set,
            reset_all_singletons,
        )

        reset_all_singletons()

        mock_load_config = MagicMock()

        try:
            # 第一阶段：安全检查（不触发副作用）
            with patch("engram.gateway.config.load_config", mock_load_config):
                container_ready = is_container_set()
                assert container_ready is False
                mock_load_config.assert_not_called()

            # 第二阶段：如果需要初始化，才调用 get_container()
            # 在无环境变量时会抛出 ConfigError
            with pytest.raises(ConfigError):
                get_container()
        finally:
            reset_all_singletons()
