"""
Test public_api.py exports using AST and runtime reflection.

This module verifies:
1. __all__ and _TIER_B_LAZY_IMPORTS consistency via AST parsing
2. __getattr__ and TYPE_CHECKING guard presence via runtime import
"""

from __future__ import annotations

import ast
from pathlib import Path


def _get_public_api_path() -> Path:
    """Get the path to public_api.py."""
    return Path(__file__).parent.parent.parent / "src" / "engram" / "gateway" / "public_api.py"


def _parse_public_api_ast() -> ast.Module:
    """Parse public_api.py and return AST module."""
    source = _get_public_api_path().read_text(encoding="utf-8")
    return ast.parse(source)


def _extract_all_from_ast(tree: ast.Module) -> list[str]:
    """Extract __all__ list from AST.

    Args:
        tree: The parsed AST module.

    Returns:
        List of exported symbol names.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, ast.List):
                        return [
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        ]
    return []


def _extract_tier_b_lazy_imports_from_ast(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Extract _TIER_B_LAZY_IMPORTS dict from AST.

    Args:
        tree: The parsed AST module.

    Returns:
        Dict mapping symbol name to (module_path, attr_name).
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "_TIER_B_LAZY_IMPORTS":
                if isinstance(node.value, ast.Dict):
                    result: dict[str, tuple[str, str]] = {}
                    for key, value in zip(node.value.keys, node.value.values):
                        if (
                            isinstance(key, ast.Constant)
                            and isinstance(key.value, str)
                            and isinstance(value, ast.Tuple)
                            and len(value.elts) == 2
                        ):
                            mod = value.elts[0]
                            attr = value.elts[1]
                            if (
                                isinstance(mod, ast.Constant)
                                and isinstance(attr, ast.Constant)
                                and isinstance(mod.value, str)
                                and isinstance(attr.value, str)
                            ):
                                result[key.value] = (mod.value, attr.value)
                    return result
    return {}


def _check_type_checking_guard_exists(tree: ast.Module) -> bool:
    """Check if TYPE_CHECKING guard exists in AST.

    Args:
        tree: The parsed AST module.

    Returns:
        True if TYPE_CHECKING guard found.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            # Check for `if TYPE_CHECKING:`
            if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                return True
    return False


def _check_getattr_function_exists(tree: ast.Module) -> bool:
    """Check if __getattr__ function exists in AST.

    Args:
        tree: The parsed AST module.

    Returns:
        True if __getattr__ function found.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            return True
    return False


# ============ AST-based Tests ============


class TestPublicApiExportsAST:
    """Test public_api.py exports using AST parsing."""

    def test_all_contains_tier_b_symbols(self) -> None:
        """Verify all Tier B symbols are in __all__."""
        tree = _parse_public_api_ast()
        all_exports = set(_extract_all_from_ast(tree))
        tier_b_symbols = set(_extract_tier_b_lazy_imports_from_ast(tree).keys())

        # All Tier B symbols must be in __all__
        missing = tier_b_symbols - all_exports
        assert not missing, f"Tier B symbols missing from __all__: {missing}"

    def test_tier_b_lazy_imports_not_empty(self) -> None:
        """Verify _TIER_B_LAZY_IMPORTS is not empty."""
        tree = _parse_public_api_ast()
        tier_b = _extract_tier_b_lazy_imports_from_ast(tree)

        assert len(tier_b) > 0, "_TIER_B_LAZY_IMPORTS should not be empty"

    def test_all_not_empty(self) -> None:
        """Verify __all__ is not empty."""
        tree = _parse_public_api_ast()
        all_exports = _extract_all_from_ast(tree)

        assert len(all_exports) > 0, "__all__ should not be empty"

    def test_tier_b_mapping_values_are_valid(self) -> None:
        """Verify _TIER_B_LAZY_IMPORTS values are valid (module_path, attr_name) tuples."""
        tree = _parse_public_api_ast()
        tier_b = _extract_tier_b_lazy_imports_from_ast(tree)

        for symbol, (module_path, attr_name) in tier_b.items():
            # Module path should start with '.'
            assert module_path.startswith("."), (
                f"{symbol}: module_path should be relative: {module_path}"
            )
            # Attr name should be non-empty
            assert attr_name, f"{symbol}: attr_name should not be empty"

    def test_type_checking_guard_exists(self) -> None:
        """Verify TYPE_CHECKING guard exists for Tier B type annotations."""
        tree = _parse_public_api_ast()
        assert _check_type_checking_guard_exists(tree), (
            "TYPE_CHECKING guard not found in public_api.py"
        )

    def test_getattr_function_exists(self) -> None:
        """Verify __getattr__ function exists for lazy imports."""
        tree = _parse_public_api_ast()
        assert _check_getattr_function_exists(tree), (
            "__getattr__ function not found in public_api.py"
        )


# ============ Runtime Reflection Tests ============


class TestPublicApiExportsRuntime:
    """Test public_api.py exports using runtime import."""

    def test_module_has_all_attribute(self) -> None:
        """Verify module has __all__ attribute."""
        from engram.gateway import public_api

        assert hasattr(public_api, "__all__"), "public_api should have __all__ attribute"
        assert isinstance(public_api.__all__, list), "__all__ should be a list"

    def test_module_has_getattr(self) -> None:
        """Verify module has __getattr__ for lazy imports."""
        from engram.gateway import public_api

        # Check __getattr__ is defined (module-level)
        # Module-level __getattr__ is accessible via module.__getattr__
        assert hasattr(public_api, "__getattr__"), (
            "public_api should have __getattr__ for lazy imports"
        )

    def test_tier_b_lazy_imports_dict_exists(self) -> None:
        """Verify _TIER_B_LAZY_IMPORTS exists at runtime."""
        from engram.gateway import public_api

        assert hasattr(public_api, "_TIER_B_LAZY_IMPORTS"), (
            "public_api should have _TIER_B_LAZY_IMPORTS"
        )
        assert isinstance(public_api._TIER_B_LAZY_IMPORTS, dict), (
            "_TIER_B_LAZY_IMPORTS should be a dict"
        )

    def test_tier_a_symbols_directly_importable(self) -> None:
        """Verify Tier A symbols are directly importable without __getattr__."""
        from engram.gateway.public_api import (
            GatewayDeps,
            GatewayDepsProtocol,
            McpErrorCategory,
            McpErrorCode,
            McpErrorReason,
            RequestContext,
            ToolResultErrorCode,
        )

        # Basic sanity checks
        assert RequestContext is not None
        assert GatewayDeps is not None
        assert GatewayDepsProtocol is not None
        assert McpErrorCode is not None
        assert McpErrorCategory is not None
        assert McpErrorReason is not None
        assert ToolResultErrorCode is not None

    def test_all_and_tier_b_consistency_at_runtime(self) -> None:
        """Verify __all__ and _TIER_B_LAZY_IMPORTS are consistent at runtime."""
        from engram.gateway import public_api

        all_exports = set(public_api.__all__)
        tier_b_symbols = set(public_api._TIER_B_LAZY_IMPORTS.keys())

        # All Tier B symbols must be in __all__
        missing = tier_b_symbols - all_exports
        assert not missing, f"Tier B symbols missing from __all__: {missing}"

    def test_mcp_rpc_symbols_in_tier_b_lazy_imports(self) -> None:
        """Verify mcp_rpc symbols are in _TIER_B_LAZY_IMPORTS."""
        from engram.gateway import public_api

        tier_b = public_api._TIER_B_LAZY_IMPORTS

        # dispatch_jsonrpc_request 和 JsonRpcDispatchResult 应在 Tier B
        assert "dispatch_jsonrpc_request" in tier_b, (
            "dispatch_jsonrpc_request should be in _TIER_B_LAZY_IMPORTS"
        )
        assert "JsonRpcDispatchResult" in tier_b, (
            "JsonRpcDispatchResult should be in _TIER_B_LAZY_IMPORTS"
        )

        # 验证映射指向 .mcp_rpc 模块
        assert tier_b["dispatch_jsonrpc_request"][0] == ".mcp_rpc"
        assert tier_b["JsonRpcDispatchResult"][0] == ".mcp_rpc"

    def test_mcp_rpc_symbols_in_all(self) -> None:
        """Verify mcp_rpc symbols are in __all__."""
        from engram.gateway import public_api

        all_exports = public_api.__all__

        assert "dispatch_jsonrpc_request" in all_exports, (
            "dispatch_jsonrpc_request should be in __all__"
        )
        assert "JsonRpcDispatchResult" in all_exports, "JsonRpcDispatchResult should be in __all__"
