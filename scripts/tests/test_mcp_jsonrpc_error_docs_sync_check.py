#!/usr/bin/env python3
"""
check_mcp_jsonrpc_error_docs_sync.py 单元测试

覆盖场景：
1. 正常解析 - 文档和 Schema 完全同步
2. error_reason 不同步：
   - 缺失 reason - Schema 中有但文档中没有
   - 额外 reason - 文档中有但 Schema 中没有
3. error_category 不同步：
   - 缺失 category - Schema 中有但文档中没有
   - 额外 category - 文档中有但 Schema 中没有
4. jsonrpc_code 不同步：
   - 缺失 code - Schema 中有但文档中没有
   - 额外 code - 文档中有但 Schema 中没有
5. 格式错误场景 - 无法解析的文档/Schema
6. 边界场景 - 空 Schema、废弃错误码过滤等
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

# 将 scripts/ci 目录添加到 path
sys.path.insert(0, str(Path(__file__).parent.parent / "ci"))

from check_mcp_jsonrpc_error_docs_sync import (
    McpErrorDocsSyncChecker,
    SyncResult,
    format_json_output,
    format_text_output,
    parse_doc_categories,
    parse_doc_jsonrpc_codes,
    parse_doc_reasons,
    parse_schema_categories,
    parse_schema_codes,
    parse_schema_reasons,
)

# ============================================================================
# Fixture 数据
# ============================================================================

# 最小有效 Markdown 文档
MINIMAL_VALID_DOC = """\
# MCP JSON-RPC Error Doc

## 2. 错误数据结构

```typescript
interface ErrorData {
  category: "protocol" | "validation" | "business" | "dependency" | "internal";
  reason: string;
}
```

## 3. 错误分类

### 3.1 protocol

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `PARSE_ERROR` | JSON 解析失败 | 否 |
| `INVALID_REQUEST` | 请求格式无效 | 否 |

### 3.2 validation

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `MISSING_REQUIRED_PARAM` | 缺少必需参数 | 否 |

### 3.3 business

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `POLICY_REJECT` | 策略拒绝 | 否 |

### 3.4 dependency

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `OPENMEMORY_UNAVAILABLE` | 服务不可用 | 是 |

### 3.5 internal

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `INTERNAL_ERROR` | 内部错误 | 否 |

## 4. JSON-RPC 错误码映射

| 错误码 | 原因码 | 分类 |
|--------|--------|------|
| -32700 | PARSE_ERROR | protocol |
| -32600 | INVALID_REQUEST | protocol |
| -32602 | MISSING_REQUIRED_PARAM | validation |
| -32001 | OPENMEMORY_UNAVAILABLE | dependency |
| -32603 | INTERNAL_ERROR | internal |
"""

# 最小有效 Schema
MINIMAL_VALID_SCHEMA = {
    "definitions": {
        "error_reason": {
            "enum": [
                "PARSE_ERROR",
                "INVALID_REQUEST",
                "MISSING_REQUIRED_PARAM",
                "POLICY_REJECT",
                "OPENMEMORY_UNAVAILABLE",
                "INTERNAL_ERROR",
            ]
        },
        "error_category": {
            "enum": ["protocol", "validation", "business", "dependency", "internal"]
        },
        "jsonrpc_error": {
            "properties": {"code": {"enum": [-32700, -32600, -32602, -32001, -32603]}}
        },
    }
}

# 缺失 reason 的文档（缺少 INTERNAL_ERROR）
DOC_MISSING_REASON = """\
# MCP JSON-RPC Error Doc

## 2. 错误数据结构

```typescript
interface ErrorData {
  category: "protocol" | "validation" | "business" | "dependency" | "internal";
  reason: string;
}
```

## 3. 错误分类

### 3.1 protocol

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `PARSE_ERROR` | JSON 解析失败 | 否 |

### 3.2 validation

### 3.3 business

### 3.4 dependency

### 3.5 internal

## 4. JSON-RPC 错误码映射

| 错误码 | 原因码 | 分类 |
|--------|--------|------|
| -32700 | PARSE_ERROR | protocol |
"""

# 额外 reason 的文档（多了 EXTRA_REASON）
DOC_EXTRA_REASON = """\
# MCP JSON-RPC Error Doc

## 2. 错误数据结构

```typescript
interface ErrorData {
  category: "protocol" | "validation" | "business" | "dependency" | "internal";
  reason: string;
}
```

## 3. 错误分类

### 3.1 protocol

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `PARSE_ERROR` | JSON 解析失败 | 否 |
| `EXTRA_REASON` | 额外的原因码 | 否 |

### 3.2 validation

### 3.3 business

### 3.4 dependency

### 3.5 internal

## 4. JSON-RPC 错误码映射

| 错误码 | 原因码 | 分类 |
|--------|--------|------|
| -32700 | PARSE_ERROR | protocol |
"""

# 格式错误的文档（缺少必要章节）
DOC_MALFORMED = """\
# MCP JSON-RPC Error Doc

Some random content without proper section markers.

| Code | Description |
|------|-------------|
| 123  | Something   |
"""

# ============================================================================
# 辅助函数
# ============================================================================


def create_temp_files(doc_content: str, schema_content: dict) -> tuple[Path, Path, Path]:
    """创建临时文档和 Schema 文件。

    Returns:
        (temp_dir, doc_path, schema_path)
    """
    temp_dir = Path(tempfile.mkdtemp())
    doc_path = temp_dir / "doc.md"
    schema_path = temp_dir / "schema.json"

    doc_path.write_text(doc_content, encoding="utf-8")
    schema_path.write_text(json.dumps(schema_content), encoding="utf-8")

    return temp_dir, doc_path, schema_path


def cleanup_temp_files(temp_dir: Path) -> None:
    """清理临时文件。"""
    import shutil

    if temp_dir.exists():
        shutil.rmtree(temp_dir)


# ============================================================================
# Test: 解析函数
# ============================================================================


class TestParseDocReasons:
    """测试文档 reason 解析"""

    def test_parse_valid_doc_reasons(self):
        """正常解析文档中的 error_reason 列表

        注意：由于 extract_section_text 的实现，同一 reason 可能被多次解析，
        但脚本在同步检查时使用 set() 去重，因此这里也使用 set 进行比较。
        """
        reasons = parse_doc_reasons(MINIMAL_VALID_DOC)
        expected = {
            "PARSE_ERROR",
            "INVALID_REQUEST",
            "MISSING_REQUIRED_PARAM",
            "POLICY_REJECT",
            "OPENMEMORY_UNAVAILABLE",
            "INTERNAL_ERROR",
        }
        assert set(reasons) == expected

    def test_parse_empty_doc(self):
        """空文档返回空列表"""
        reasons = parse_doc_reasons("")
        assert reasons == []

    def test_parse_doc_without_reason_sections(self):
        """缺少 reason 章节的文档返回空列表"""
        reasons = parse_doc_reasons(DOC_MALFORMED)
        assert reasons == []


class TestParseDocCategories:
    """测试文档 category 解析"""

    def test_parse_valid_doc_categories(self):
        """正常解析文档中的 error_category 列表"""
        categories = parse_doc_categories(MINIMAL_VALID_DOC)
        expected = ["protocol", "validation", "business", "dependency", "internal"]
        assert sorted(categories) == sorted(expected)

    def test_parse_empty_doc(self):
        """空文档返回空列表"""
        categories = parse_doc_categories("")
        assert categories == []


class TestParseDocJsonrpcCodes:
    """测试文档 JSON-RPC 错误码解析"""

    def test_parse_valid_doc_codes(self):
        """正常解析文档中的 JSON-RPC 错误码"""
        codes = parse_doc_jsonrpc_codes(MINIMAL_VALID_DOC)
        expected = [-32700, -32600, -32602, -32001, -32603]
        assert sorted(codes) == sorted(expected)

    def test_parse_empty_doc(self):
        """空文档返回空列表"""
        codes = parse_doc_jsonrpc_codes("")
        assert codes == []


class TestParseSchema:
    """测试 Schema 解析"""

    def test_parse_schema_reasons(self):
        """正常解析 Schema 中的 error_reason"""
        reasons = parse_schema_reasons(MINIMAL_VALID_SCHEMA)
        assert len(reasons) == 6
        assert "PARSE_ERROR" in reasons

    def test_parse_schema_categories(self):
        """正常解析 Schema 中的 error_category"""
        categories = parse_schema_categories(MINIMAL_VALID_SCHEMA)
        assert sorted(categories) == sorted(
            ["protocol", "validation", "business", "dependency", "internal"]
        )

    def test_parse_schema_codes(self):
        """正常解析 Schema 中的 JSON-RPC 错误码"""
        codes = parse_schema_codes(MINIMAL_VALID_SCHEMA)
        assert sorted(codes) == sorted([-32700, -32600, -32602, -32001, -32603])

    def test_parse_empty_schema(self):
        """空 Schema 返回空列表"""
        empty_schema: dict = {"definitions": {}}
        assert parse_schema_reasons(empty_schema) == []
        assert parse_schema_categories(empty_schema) == []
        assert parse_schema_codes(empty_schema) == []


# ============================================================================
# Test: McpErrorDocsSyncChecker
# ============================================================================


class TestMcpErrorDocsSyncChecker:
    """测试同步校验器"""

    def test_sync_check_pass(self):
        """文档和 Schema 完全同步时校验通过"""
        temp_dir, doc_path, schema_path = create_temp_files(MINIMAL_VALID_DOC, MINIMAL_VALID_SCHEMA)
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is True
            assert len(result.errors) == 0
        finally:
            cleanup_temp_files(temp_dir)

    def test_sync_check_missing_reason_in_doc(self):
        """文档缺少 reason 时报错"""
        # Schema 有 INTERNAL_ERROR，但文档中没有
        schema_with_extra = {
            "definitions": {
                "error_reason": {"enum": ["PARSE_ERROR", "INTERNAL_ERROR"]},
                "error_category": {"enum": ["protocol", "internal"]},
                "jsonrpc_error": {"properties": {"code": {"enum": [-32700]}}},
            }
        }
        temp_dir, doc_path, schema_path = create_temp_files(DOC_MISSING_REASON, schema_with_extra)
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is False
            # 应该有 missing_in_doc 错误
            missing_errors = [e for e in result.errors if e.error_type == "missing_in_doc"]
            assert len(missing_errors) >= 1
            # INTERNAL_ERROR 应该在缺失列表中
            missing_values = [e.value for e in missing_errors]
            assert "INTERNAL_ERROR" in missing_values
        finally:
            cleanup_temp_files(temp_dir)

    def test_sync_check_extra_reason_in_doc(self):
        """文档有额外 reason 时报错"""
        # Schema 只有 PARSE_ERROR，但文档多了 EXTRA_REASON
        schema_minimal = {
            "definitions": {
                "error_reason": {"enum": ["PARSE_ERROR"]},
                "error_category": {"enum": ["protocol"]},
                "jsonrpc_error": {"properties": {"code": {"enum": [-32700]}}},
            }
        }
        temp_dir, doc_path, schema_path = create_temp_files(DOC_EXTRA_REASON, schema_minimal)
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is False
            # 应该有 extra_in_doc 错误
            extra_errors = [e for e in result.errors if e.error_type == "extra_in_doc"]
            assert len(extra_errors) >= 1
            extra_values = [e.value for e in extra_errors]
            assert "EXTRA_REASON" in extra_values
        finally:
            cleanup_temp_files(temp_dir)

    def test_sync_check_file_not_found(self):
        """文件不存在时报错"""
        non_existent_doc = Path("/nonexistent/doc.md")
        non_existent_schema = Path("/nonexistent/schema.json")

        checker = McpErrorDocsSyncChecker(non_existent_doc, non_existent_schema)
        result = checker.check()

        assert result.success is False
        file_errors = [e for e in result.errors if e.category == "file"]
        assert len(file_errors) >= 1

    def test_sync_check_invalid_json_schema(self):
        """Schema JSON 格式错误时报错"""
        temp_dir = Path(tempfile.mkdtemp())
        doc_path = temp_dir / "doc.md"
        schema_path = temp_dir / "schema.json"

        doc_path.write_text(MINIMAL_VALID_DOC, encoding="utf-8")
        schema_path.write_text("{ invalid json }", encoding="utf-8")

        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is False
            file_errors = [e for e in result.errors if e.category == "file"]
            assert len(file_errors) >= 1
        finally:
            cleanup_temp_files(temp_dir)


# ============================================================================
# Test: 输出格式
# ============================================================================


class TestOutputFormats:
    """测试输出格式"""

    def test_format_json_output_success(self):
        """JSON 输出格式 - 成功场景"""
        result = SyncResult()
        result.schema_reasons = ["PARSE_ERROR"]
        result.doc_reasons = ["PARSE_ERROR"]
        result.schema_categories = ["protocol"]
        result.doc_categories = ["protocol"]
        result.schema_codes = [-32700]
        result.doc_codes = [-32700]

        output = format_json_output(result)
        data = json.loads(output)

        assert data["success"] is True
        assert data["error_count"] == 0
        assert data["schema_error_reasons"] == ["PARSE_ERROR"]
        assert data["doc_error_reasons"] == ["PARSE_ERROR"]

    def test_format_json_output_failure(self):
        """JSON 输出格式 - 失败场景"""
        result = SyncResult()
        result.success = False
        from check_mcp_jsonrpc_error_docs_sync import SyncError

        result.errors.append(
            SyncError(
                error_type="missing_in_doc",
                category="error_reason",
                value="INTERNAL_ERROR",
                message="Reason 'INTERNAL_ERROR' exists in schema but not documented",
                fix_suggestion="Add to section 3.5",
            )
        )

        output = format_json_output(result)
        data = json.loads(output)

        assert data["success"] is False
        assert data["error_count"] == 1
        assert len(data["errors"]) == 1
        assert data["errors"][0]["error_type"] == "missing_in_doc"
        assert data["errors"][0]["value"] == "INTERNAL_ERROR"

    def test_format_text_output_success(self):
        """文本输出格式 - 成功场景"""
        result = SyncResult()
        result.schema_reasons = ["PARSE_ERROR"]
        result.doc_reasons = ["PARSE_ERROR"]

        output = format_text_output(result)

        assert "PASSED" in output
        assert "Errors: 0" in output

    def test_format_text_output_failure(self):
        """文本输出格式 - 失败场景"""
        result = SyncResult()
        result.success = False
        from check_mcp_jsonrpc_error_docs_sync import SyncError

        result.errors.append(
            SyncError(
                error_type="extra_in_doc",
                category="error_reason",
                value="EXTRA_REASON",
                message="Reason 'EXTRA_REASON' documented but not in schema",
            )
        )

        output = format_text_output(result)

        assert "FAILED" in output
        assert "EXTRA_REASON" in output


# ============================================================================
# Test: 退出码
# ============================================================================


class TestExitCodes:
    """测试退出码逻辑"""

    def test_exit_code_zero_on_success(self):
        """校验通过时退出码为 0"""
        temp_dir, doc_path, schema_path = create_temp_files(MINIMAL_VALID_DOC, MINIMAL_VALID_SCHEMA)
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is True
            # 模拟 main() 中的退出码逻辑
            exit_code = (
                0
                if result.success
                else (2 if any(e.category == "file" for e in result.errors) else 1)
            )
            assert exit_code == 0
        finally:
            cleanup_temp_files(temp_dir)

    def test_exit_code_one_on_sync_failure(self):
        """同步校验失败时退出码为 1"""
        schema_with_extra = {
            "definitions": {
                "error_reason": {"enum": ["PARSE_ERROR", "UNKNOWN_EXTRA"]},
                "error_category": {"enum": ["protocol"]},
                "jsonrpc_error": {"properties": {"code": {"enum": [-32700]}}},
            }
        }
        temp_dir, doc_path, schema_path = create_temp_files(DOC_MISSING_REASON, schema_with_extra)
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is False
            # 模拟 main() 中的退出码逻辑
            file_errors = [e for e in result.errors if e.category == "file"]
            exit_code = 2 if file_errors else 1
            assert exit_code == 1
        finally:
            cleanup_temp_files(temp_dir)

    def test_exit_code_two_on_file_error(self):
        """文件错误时退出码为 2"""
        non_existent_doc = Path("/nonexistent/doc.md")
        non_existent_schema = Path("/nonexistent/schema.json")

        checker = McpErrorDocsSyncChecker(non_existent_doc, non_existent_schema)
        result = checker.check()

        assert result.success is False
        file_errors = [e for e in result.errors if e.category == "file"]
        exit_code = 2 if file_errors else 1
        assert exit_code == 2


# ============================================================================
# Test: 边界场景
# ============================================================================


class TestCategorySyncErrors:
    """测试 error_category 不同步场景"""

    def test_sync_check_missing_category_in_doc(self):
        """文档缺少 category 时报错"""
        # 文档只有 protocol, validation，但 Schema 有 protocol, validation, business
        doc_missing_category = """\
# MCP JSON-RPC Error Doc

## 2. 错误数据结构

```typescript
interface ErrorData {
  category: "protocol" | "validation";
  reason: string;
}
```

## 3. 错误分类

### 3.1 protocol

|| 原因码 | 说明 | 可重试 |
||--------|------|--------|
|| `PARSE_ERROR` | JSON 解析失败 | 否 |

### 3.2 validation

### 3.3 business

### 3.4 dependency

### 3.5 internal

## 4. JSON-RPC 错误码映射

|| 错误码 | 原因码 | 分类 |
||--------|--------|------|
|| -32700 | PARSE_ERROR | protocol |
"""
        schema_with_more_categories = {
            "definitions": {
                "error_reason": {"enum": ["PARSE_ERROR"]},
                "error_category": {"enum": ["protocol", "validation", "business"]},
                "jsonrpc_error": {"properties": {"code": {"enum": [-32700]}}},
            }
        }
        temp_dir, doc_path, schema_path = create_temp_files(
            doc_missing_category, schema_with_more_categories
        )
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is False
            missing_errors = [
                e
                for e in result.errors
                if e.error_type == "missing_in_doc" and e.category == "error_category"
            ]
            assert len(missing_errors) >= 1
            missing_values = [e.value for e in missing_errors]
            assert "business" in missing_values
        finally:
            cleanup_temp_files(temp_dir)

    def test_sync_check_extra_category_in_doc(self):
        """文档有额外 category 时报错"""
        # 文档有 internal，但 Schema 只有 protocol
        doc_extra_category = """\
# MCP JSON-RPC Error Doc

## 2. 错误数据结构

```typescript
interface ErrorData {
  category: "protocol" | "internal";
  reason: string;
}
```

## 3. 错误分类

### 3.1 protocol

|| 原因码 | 说明 | 可重试 |
||--------|------|--------|
|| `PARSE_ERROR` | JSON 解析失败 | 否 |

### 3.2 validation

### 3.3 business

### 3.4 dependency

### 3.5 internal

## 4. JSON-RPC 错误码映射

|| 错误码 | 原因码 | 分类 |
||--------|--------|------|
|| -32700 | PARSE_ERROR | protocol |
"""
        schema_minimal_category = {
            "definitions": {
                "error_reason": {"enum": ["PARSE_ERROR"]},
                "error_category": {"enum": ["protocol"]},
                "jsonrpc_error": {"properties": {"code": {"enum": [-32700]}}},
            }
        }
        temp_dir, doc_path, schema_path = create_temp_files(
            doc_extra_category, schema_minimal_category
        )
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is False
            extra_errors = [
                e
                for e in result.errors
                if e.error_type == "extra_in_doc" and e.category == "error_category"
            ]
            assert len(extra_errors) >= 1
            extra_values = [e.value for e in extra_errors]
            assert "internal" in extra_values
        finally:
            cleanup_temp_files(temp_dir)


class TestJsonrpcCodeSyncErrors:
    """测试 JSON-RPC 错误码不同步场景"""

    def test_sync_check_missing_code_in_doc(self):
        """文档缺少 JSON-RPC 错误码时报错"""
        # 文档只有 -32700，但 Schema 有 -32700, -32600
        doc_missing_code = """\
# MCP JSON-RPC Error Doc

## 2. 错误数据结构

```typescript
interface ErrorData {
  category: "protocol";
  reason: string;
}
```

## 3. 错误分类

### 3.1 protocol

|| 原因码 | 说明 | 可重试 |
||--------|------|--------|
|| `PARSE_ERROR` | JSON 解析失败 | 否 |

### 3.2 validation

### 3.3 business

### 3.4 dependency

### 3.5 internal

## 4. JSON-RPC 错误码映射

|| 错误码 | 原因码 | 分类 |
||--------|--------|------|
|| -32700 | PARSE_ERROR | protocol |
"""
        schema_with_more_codes = {
            "definitions": {
                "error_reason": {"enum": ["PARSE_ERROR"]},
                "error_category": {"enum": ["protocol"]},
                "jsonrpc_error": {"properties": {"code": {"enum": [-32700, -32600]}}},
            }
        }
        temp_dir, doc_path, schema_path = create_temp_files(
            doc_missing_code, schema_with_more_codes
        )
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is False
            missing_errors = [
                e
                for e in result.errors
                if e.error_type == "missing_in_doc" and e.category == "jsonrpc_code"
            ]
            assert len(missing_errors) >= 1
            missing_values = [e.value for e in missing_errors]
            assert "-32600" in missing_values
        finally:
            cleanup_temp_files(temp_dir)

    def test_sync_check_extra_code_in_doc(self):
        """文档有额外 JSON-RPC 错误码时报错"""
        # 文档有 -32700, -32601，但 Schema 只有 -32700
        doc_extra_code = """\
# MCP JSON-RPC Error Doc

## 2. 错误数据结构

```typescript
interface ErrorData {
  category: "protocol";
  reason: string;
}
```

## 3. 错误分类

### 3.1 protocol

|| 原因码 | 说明 | 可重试 |
||--------|------|--------|
|| `PARSE_ERROR` | JSON 解析失败 | 否 |

### 3.2 validation

### 3.3 business

### 3.4 dependency

### 3.5 internal

## 4. JSON-RPC 错误码映射

|| 错误码 | 原因码 | 分类 |
||--------|--------|------|
|| -32700 | PARSE_ERROR | protocol |
|| -32601 | METHOD_NOT_FOUND | protocol |
"""
        schema_minimal_code = {
            "definitions": {
                "error_reason": {"enum": ["PARSE_ERROR"]},
                "error_category": {"enum": ["protocol"]},
                "jsonrpc_error": {"properties": {"code": {"enum": [-32700]}}},
            }
        }
        temp_dir, doc_path, schema_path = create_temp_files(
            doc_extra_code, schema_minimal_code
        )
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            assert result.success is False
            extra_errors = [
                e
                for e in result.errors
                if e.error_type == "extra_in_doc" and e.category == "jsonrpc_code"
            ]
            assert len(extra_errors) >= 1
            extra_values = [e.value for e in extra_errors]
            assert "-32601" in extra_values
        finally:
            cleanup_temp_files(temp_dir)


class TestEdgeCases:
    """测试边界场景"""

    def test_empty_schema_definitions(self):
        """空 definitions 处理"""
        empty_schema: dict = {"definitions": {}}
        temp_dir, doc_path, schema_path = create_temp_files(MINIMAL_VALID_DOC, empty_schema)
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            # 文档有 reason，但 Schema 为空，应该报 extra_in_doc
            assert result.success is False
            extra_errors = [e for e in result.errors if e.error_type == "extra_in_doc"]
            assert len(extra_errors) > 0
        finally:
            cleanup_temp_files(temp_dir)

    def test_schema_without_definitions_key(self):
        """缺少 definitions 键的 Schema"""
        no_def_schema: dict = {"$schema": "https://json-schema.org/..."}
        temp_dir, doc_path, schema_path = create_temp_files(MINIMAL_VALID_DOC, no_def_schema)
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            # Schema 没有 definitions，所有文档内容都是 "extra"
            assert result.success is False
        finally:
            cleanup_temp_files(temp_dir)

    def test_doc_with_deprecated_code_filtered(self):
        """废弃的错误码应被过滤"""
        doc_with_deprecated = """\
# MCP JSON-RPC Error Doc

## 4. JSON-RPC 错误码映射

| 错误码 | 原因码 | 分类 | 备注 |
|--------|--------|------|------|
| -32700 | PARSE_ERROR | protocol | |
| -32000 | TOOL_EXECUTION_ERROR | internal | 废弃 |
"""
        schema_without_deprecated = {
            "definitions": {
                "error_reason": {"enum": ["PARSE_ERROR"]},
                "error_category": {"enum": ["protocol"]},
                "jsonrpc_error": {"properties": {"code": {"enum": [-32700]}}},
            }
        }
        temp_dir, doc_path, schema_path = create_temp_files(
            doc_with_deprecated, schema_without_deprecated
        )
        try:
            checker = McpErrorDocsSyncChecker(doc_path, schema_path)
            result = checker.check()

            # -32000 应被过滤，不应报错
            code_errors = [
                e for e in result.errors if e.category == "jsonrpc_code" and e.value == "-32000"
            ]
            assert len(code_errors) == 0
        finally:
            cleanup_temp_files(temp_dir)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
