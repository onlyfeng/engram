"""
tests/ci/test_gateway_deps_db_allowlist.py

测试 scripts/ci/check_gateway_deps_db_allowlist.py 的核心功能:
1. JSON Schema 校验
2. 重复 id 校验
3. expires_on 日期格式与过期校验
4. owner 字段校验
5. category 字段校验
6. reason 长度校验
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.ci.check_gateway_deps_db_allowlist import (
    DATE_PATTERN,
    ID_PATTERN,
    OWNER_PATTERN,
    VALID_CATEGORIES,
    ValidationError,
    ValidationResult,
    get_entry_required_fields,
    get_entry_valid_fields,
    load_json_file,
    validate_against_schema,
    validate_allowlist,
    validate_category,
    validate_date_format,
    validate_entry_fields,
    validate_expires_on,
    validate_id_format,
    validate_owner,
    validate_reason_length,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_allowlist_file(tmp_path: Path) -> Path:
    """创建临时 allowlist 文件路径。"""
    return tmp_path / "allowlist.json"


@pytest.fixture
def temp_schema_file(tmp_path: Path) -> Path:
    """创建临时 schema 文件路径。"""
    return tmp_path / "schema.json"


@pytest.fixture
def valid_allowlist_data() -> dict:
    """有效的 allowlist 数据。"""
    future_date = (date.today() + timedelta(days=365)).isoformat()
    return {
        "version": "1",
        "entries": [
            {
                "id": "adapter-internal-audit-v1",
                "file_glob": "src/engram/gateway/services/audit_service.py",
                "reason": "Audit service 内部需要直接访问连接进行批量写入优化",
                "owner": "@platform-team",
                "expires_on": future_date,
                "category": "adapter_internal",
            }
        ],
    }


@pytest.fixture
def minimal_schema() -> dict:
    """最小 schema 定义。"""
    return {
        "definitions": {
            "allowlist_entry": {
                "required": ["id", "file_glob", "reason", "owner", "category", "expires_on"],
                "properties": {
                    "id": {"type": "string"},
                    "file_glob": {"type": "string"},
                    "file_path": {"type": "string"},
                    "reason": {"type": "string"},
                    "owner": {"type": "string"},
                    "expires_on": {"type": "string"},
                    "category": {"type": "string"},
                    "created_at": {"type": "string"},
                    "ticket": {"type": "string"},
                    "notes": {"type": "string"},
                    "migration_target": {"type": "string"},
                },
            }
        }
    }


# ============================================================================
# 日期格式校验测试
# ============================================================================


class TestDateFormat:
    """测试日期格式校验。"""

    def test_valid_date_format(self) -> None:
        """有效的 YYYY-MM-DD 格式应通过。"""
        assert validate_date_format("2026-12-31")
        assert validate_date_format("2025-01-01")
        assert validate_date_format("2030-06-15")

    def test_invalid_date_format_wrong_separator(self) -> None:
        """错误的分隔符应失败。"""
        assert not validate_date_format("2026/12/31")
        assert not validate_date_format("2026.12.31")

    def test_invalid_date_format_wrong_order(self) -> None:
        """错误的日期顺序应失败。"""
        assert not validate_date_format("31-12-2026")
        assert not validate_date_format("12-31-2026")

    def test_invalid_date_format_incomplete(self) -> None:
        """不完整的日期应失败。"""
        assert not validate_date_format("2026-12")
        assert not validate_date_format("2026")

    def test_invalid_date_nonexistent(self) -> None:
        """不存在的日期应失败。"""
        assert not validate_date_format("2026-02-30")
        assert not validate_date_format("2026-13-01")

    def test_date_pattern_regex(self) -> None:
        """DATE_PATTERN 正则应正确匹配。"""
        assert DATE_PATTERN.match("2026-12-31")
        assert DATE_PATTERN.match("1999-01-01")
        assert not DATE_PATTERN.match("2026-1-31")  # 单数字月份
        assert not DATE_PATTERN.match("26-12-31")  # 两位数年份


# ============================================================================
# Owner 格式校验测试
# ============================================================================


class TestOwnerFormat:
    """测试 owner 字段格式校验。"""

    def test_valid_owner_with_at_sign(self) -> None:
        """带 @ 的用户名应通过。"""
        assert OWNER_PATTERN.match("@engram-team")
        assert OWNER_PATTERN.match("@user123")
        assert OWNER_PATTERN.match("@my-team-name")
        assert OWNER_PATTERN.match("@platform-team")

    def test_invalid_owner_without_at_sign(self) -> None:
        """不带 @ 的用户名应失败（本 schema 要求必须以 @ 开头）。"""
        assert not OWNER_PATTERN.match("engram-team")
        assert not OWNER_PATTERN.match("user123")

    def test_invalid_owner_with_spaces(self) -> None:
        """包含空格的应失败。"""
        assert not OWNER_PATTERN.match("@engram team")
        assert not OWNER_PATTERN.match("user name")

    def test_invalid_owner_special_chars(self) -> None:
        """包含特殊字符的应失败。"""
        assert not OWNER_PATTERN.match("@user@team")
        assert not OWNER_PATTERN.match("@user.name")


class TestValidateOwner:
    """测试 validate_owner 函数。"""

    def test_valid_owner(self) -> None:
        """有效的 owner 应通过。"""
        result = ValidationResult()
        entry = {"owner": "@engram-team"}
        assert validate_owner(entry, "test-id", result)
        assert len(result.errors) == 0

    def test_empty_owner(self) -> None:
        """空 owner 应报错。"""
        result = ValidationResult()
        entry = {"owner": ""}
        assert not validate_owner(entry, "test-id", result)
        assert len(result.errors) == 1
        assert "owner" in result.errors[0].field

    def test_missing_owner(self) -> None:
        """缺少 owner 应报错。"""
        result = ValidationResult()
        entry = {}
        assert not validate_owner(entry, "test-id", result)
        assert len(result.errors) == 1

    def test_whitespace_only_owner(self) -> None:
        """纯空白 owner 应报错。"""
        result = ValidationResult()
        entry = {"owner": "   "}
        assert not validate_owner(entry, "test-id", result)
        assert len(result.errors) == 1

    def test_owner_without_at_fails(self) -> None:
        """不带 @ 的 owner 应报错。"""
        result = ValidationResult()
        entry = {"owner": "engram-team"}  # 缺少 @
        assert not validate_owner(entry, "test-id", result)
        assert len(result.errors) == 1
        assert "格式无效" in result.errors[0].message


# ============================================================================
# 过期日期校验测试
# ============================================================================


class TestValidateExpiresOn:
    """测试 validate_expires_on 函数。"""

    def test_future_date_passes(self) -> None:
        """未来日期应通过。"""
        result = ValidationResult()
        future_date = (date.today() + timedelta(days=30)).isoformat()
        entry = {"expires_on": future_date}
        assert validate_expires_on(entry, "test-id", result)
        assert len(result.errors) == 0
        assert len(result.expired_entries) == 0

    def test_past_date_fails(self) -> None:
        """过去日期应报错。"""
        result = ValidationResult()
        # 使用固定日期避免 UTC 与本地时区差异
        fixed_today = date(2026, 2, 2)
        past_date = (fixed_today - timedelta(days=1)).isoformat()
        entry = {"expires_on": past_date}
        assert not validate_expires_on(entry, "test-id", result, today=fixed_today)
        assert len(result.errors) == 1
        assert "test-id" in result.expired_entries

    def test_today_date_passes(self) -> None:
        """今天日期应通过（不视为过期）。"""
        result = ValidationResult()
        today = date.today().isoformat()
        entry = {"expires_on": today}
        assert validate_expires_on(entry, "test-id", result)
        assert len(result.expired_entries) == 0

    def test_invalid_format_fails(self) -> None:
        """无效格式应报错。"""
        result = ValidationResult()
        entry = {"expires_on": "2026/12/31"}
        assert not validate_expires_on(entry, "test-id", result)
        assert len(result.errors) == 1
        assert "格式" in result.errors[0].message

    def test_missing_expires_on_fails(self) -> None:
        """缺少 expires_on 应报错（必需字段）。"""
        result = ValidationResult()
        entry = {}
        assert not validate_expires_on(entry, "test-id", result)
        assert len(result.errors) == 1

    def test_custom_today_date(self) -> None:
        """使用自定义 today 日期进行测试。"""
        result = ValidationResult()
        entry = {"expires_on": "2026-06-01"}
        # 2026-06-01 之前，应通过
        assert validate_expires_on(entry, "test-id", result, today=date(2026, 1, 1))
        assert len(result.errors) == 0

        # 2026-06-02，应失败
        result2 = ValidationResult()
        assert not validate_expires_on(entry, "test-id", result2, today=date(2026, 6, 2))
        assert len(result2.errors) == 1

    def test_boundary_today_equals_expires_not_expired(self) -> None:
        """边界条件: today == expires 时不应视为过期。

        语义: today > expires 才算过期，today == expires 仍有效。
        """
        result = ValidationResult()
        entry = {"expires_on": "2026-06-15"}
        # today == expires 时，应通过（不算过期）
        assert validate_expires_on(entry, "test-id", result, today=date(2026, 6, 15))
        assert len(result.errors) == 0
        assert len(result.expired_entries) == 0

    def test_boundary_today_equals_expires_plus_one_is_expired(self) -> None:
        """边界条件: today == expires+1 时应视为过期。

        语义: today > expires 才算过期。
        """
        result = ValidationResult()
        entry = {"expires_on": "2026-06-15"}
        # today == expires+1 时，应失败（过期）
        assert not validate_expires_on(entry, "test-id", result, today=date(2026, 6, 16))
        assert len(result.errors) == 1
        assert "test-id" in result.expired_entries
        assert "过期" in result.errors[0].message


# ============================================================================
# ID 格式校验测试
# ============================================================================


class TestValidateIdFormat:
    """测试 validate_id_format 函数。"""

    def test_valid_id(self) -> None:
        """有效的 id 应通过。"""
        result = ValidationResult()
        entry = {"id": "adapter-internal-audit-v1"}
        assert validate_id_format(entry, "adapter-internal-audit-v1", result)
        assert len(result.errors) == 0

    def test_empty_id_fails(self) -> None:
        """空 id 应报错。"""
        result = ValidationResult()
        entry = {"id": ""}
        assert not validate_id_format(entry, "", result)
        assert len(result.errors) == 1

    def test_uppercase_id_fails(self) -> None:
        """大写 id 应报错。"""
        result = ValidationResult()
        entry = {"id": "Import-DB-Test"}
        assert not validate_id_format(entry, "Import-DB-Test", result)
        assert len(result.errors) == 1
        assert "格式无效" in result.errors[0].message

    def test_id_pattern_regex(self) -> None:
        """ID_PATTERN 正则应正确匹配。"""
        assert ID_PATTERN.match("adapter-internal-audit-v1")
        assert ID_PATTERN.match("test_entry_123")
        assert ID_PATTERN.match("simple")
        assert not ID_PATTERN.match("Has-Uppercase")
        assert not ID_PATTERN.match("has spaces")
        assert not ID_PATTERN.match("has.dots")


# ============================================================================
# Category 校验测试
# ============================================================================


class TestValidateCategory:
    """测试 validate_category 函数。"""

    def test_valid_categories(self) -> None:
        """有效的 category 应通过。"""
        for category in VALID_CATEGORIES:
            result = ValidationResult()
            entry = {"category": category}
            assert validate_category(entry, "test-id", result)
            assert len(result.errors) == 0

    def test_invalid_category_fails(self) -> None:
        """无效的 category 应报错。"""
        result = ValidationResult()
        entry = {"category": "invalid_category"}
        assert not validate_category(entry, "test-id", result)
        assert len(result.errors) == 1
        assert "category" in result.errors[0].field

    def test_missing_category_fails(self) -> None:
        """缺少 category 应报错。"""
        result = ValidationResult()
        entry = {}
        assert not validate_category(entry, "test-id", result)
        assert len(result.errors) == 1


# ============================================================================
# Reason 长度校验测试
# ============================================================================


class TestValidateReasonLength:
    """测试 validate_reason_length 函数。"""

    def test_valid_reason_length(self) -> None:
        """足够长的 reason 应通过。"""
        result = ValidationResult()
        entry = {"reason": "This is a valid reason that is long enough"}
        assert validate_reason_length(entry, "test-id", result)
        assert len(result.errors) == 0

    def test_short_reason_fails(self) -> None:
        """太短的 reason 应报错。"""
        result = ValidationResult()
        entry = {"reason": "short"}
        assert not validate_reason_length(entry, "test-id", result)
        assert len(result.errors) == 1
        assert "长度" in result.errors[0].message

    def test_missing_reason_fails(self) -> None:
        """缺少 reason 应报错。"""
        result = ValidationResult()
        entry = {}
        assert not validate_reason_length(entry, "test-id", result)
        assert len(result.errors) == 1


# ============================================================================
# Schema 校验测试
# ============================================================================


class TestValidateAgainstSchema:
    """测试 validate_against_schema 函数。"""

    def test_valid_data(self, minimal_schema: dict) -> None:
        """有效数据应通过。"""
        result = ValidationResult()
        data = {"version": "1", "entries": []}
        assert validate_against_schema(data, minimal_schema, result)
        assert len(result.schema_errors) == 0

    def test_missing_version(self, minimal_schema: dict) -> None:
        """缺少 version 应报错。"""
        result = ValidationResult()
        data = {"entries": []}
        assert not validate_against_schema(data, minimal_schema, result)
        assert any("version" in e for e in result.schema_errors)

    def test_missing_entries(self, minimal_schema: dict) -> None:
        """缺少 entries 应报错。"""
        result = ValidationResult()
        data = {"version": "1"}
        assert not validate_against_schema(data, minimal_schema, result)
        assert any("entries" in e for e in result.schema_errors)

    def test_invalid_version(self, minimal_schema: dict) -> None:
        """无效版本号应报错。"""
        result = ValidationResult()
        data = {"version": "2.0", "entries": []}
        assert not validate_against_schema(data, minimal_schema, result)
        assert any("version" in e for e in result.schema_errors)

    def test_version_1_0_accepted(self, minimal_schema: dict) -> None:
        """版本 1.0 应被接受。"""
        result = ValidationResult()
        data = {"version": "1.0", "entries": []}
        assert validate_against_schema(data, minimal_schema, result)


# ============================================================================
# 条目字段校验测试
# ============================================================================


class TestValidateEntryFields:
    """测试 validate_entry_fields 函数。"""

    def test_all_required_fields_present(self) -> None:
        """所有必需字段存在应通过。"""
        result = ValidationResult()
        entry = {
            "id": "test-1",
            "file_glob": "src/*.py",
            "reason": "Test reason with enough length",
            "owner": "@team",
            "category": "testing",
            "expires_on": "2026-12-31",
        }
        required = ["id", "file_glob", "reason", "owner", "category", "expires_on"]
        valid_fields = set(entry.keys())
        assert validate_entry_fields(entry, "test-1", required, valid_fields, result)
        assert len(result.errors) == 0

    def test_missing_required_field(self) -> None:
        """缺少必需字段应报错。"""
        result = ValidationResult()
        entry = {"id": "test-1", "file_glob": "src/*.py"}
        required = ["id", "file_glob", "reason", "owner", "category", "expires_on"]
        valid_fields = {"id", "file_glob", "reason", "owner", "category", "expires_on"}
        assert not validate_entry_fields(entry, "test-1", required, valid_fields, result)
        assert len(result.errors) >= 1

    def test_unknown_field_warns(self) -> None:
        """未知字段应产生警告。"""
        result = ValidationResult()
        entry = {
            "id": "test-1",
            "file_glob": "src/*.py",
            "reason": "Test reason",
            "owner": "@team",
            "category": "testing",
            "expires_on": "2026-12-31",
            "unknown_field": "value",
        }
        required = ["id", "file_glob", "reason", "owner", "category", "expires_on"]
        valid_fields = {"id", "file_glob", "reason", "owner", "category", "expires_on"}
        validate_entry_fields(entry, "test-1", required, valid_fields, result)
        assert len(result.warnings) == 1
        assert "unknown_field" in result.warnings[0].field


# ============================================================================
# JSON 文件加载测试
# ============================================================================


class TestLoadJsonFile:
    """测试 load_json_file 函数。"""

    def test_load_valid_json(self, tmp_path: Path) -> None:
        """加载有效 JSON 应成功。"""
        json_file = tmp_path / "test.json"
        json_file.write_text('{"key": "value"}', encoding="utf-8")
        data, error = load_json_file(json_file)
        assert data == {"key": "value"}
        assert error is None

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        """加载不存在的文件应返回错误。"""
        json_file = tmp_path / "nonexistent.json"
        data, error = load_json_file(json_file)
        assert data is None
        assert "不存在" in error

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        """加载无效 JSON 应返回错误。"""
        json_file = tmp_path / "invalid.json"
        json_file.write_text("{ invalid json }", encoding="utf-8")
        data, error = load_json_file(json_file)
        assert data is None
        assert "解析失败" in error


# ============================================================================
# Schema 字段提取测试
# ============================================================================


class TestSchemaFieldExtraction:
    """测试从 schema 提取字段的函数。"""

    def test_get_entry_required_fields(self, minimal_schema: dict) -> None:
        """应正确提取必需字段。"""
        required = get_entry_required_fields(minimal_schema)
        assert "id" in required
        assert "file_glob" in required
        assert "owner" in required
        assert "reason" in required
        assert "category" in required
        assert "expires_on" in required

    def test_get_entry_valid_fields(self, minimal_schema: dict) -> None:
        """应正确提取有效字段。"""
        valid_fields = get_entry_valid_fields(minimal_schema)
        assert "id" in valid_fields
        assert "file_glob" in valid_fields
        assert "expires_on" in valid_fields
        assert "notes" in valid_fields
        assert "migration_target" in valid_fields


# ============================================================================
# 完整校验集成测试
# ============================================================================


class TestValidateAllowlist:
    """测试 validate_allowlist 完整校验。"""

    def test_valid_allowlist(
        self,
        tmp_path: Path,
        valid_allowlist_data: dict,
        minimal_schema: dict,
    ) -> None:
        """有效 allowlist 应通过校验。"""
        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(valid_allowlist_data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file)
        assert not result.has_errors()
        assert result.entries_checked == 1

    def test_expired_entry_fails(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """过期条目应导致校验失败。"""
        # 使用固定日期避免 UTC 与本地时区差异
        fixed_today = date(2026, 2, 2)
        past_date = (fixed_today - timedelta(days=1)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "expired-entry",
                    "file_glob": "src/*.py",
                    "reason": "Test reason for expired entry",
                    "owner": "@team",
                    "expires_on": past_date,
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file, today=fixed_today)
        assert result.has_errors()
        assert "expired-entry" in result.expired_entries

    def test_missing_owner_fails(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """缺少 owner 应导致校验失败。"""
        future_date = (date.today() + timedelta(days=30)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "no-owner-entry",
                    "file_glob": "src/*.py",
                    "reason": "Test reason for entry",
                    "expires_on": future_date,
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file)
        assert result.has_errors()
        assert any("owner" in e.field for e in result.errors)

    def test_duplicate_ids_fail(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """重复 id 应导致校验失败。"""
        future_date = (date.today() + timedelta(days=30)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "same-id",
                    "file_glob": "src/a.py",
                    "reason": "First entry reason",
                    "owner": "@team",
                    "expires_on": future_date,
                    "category": "testing",
                },
                {
                    "id": "same-id",
                    "file_glob": "src/b.py",
                    "reason": "Second entry reason",
                    "owner": "@team",
                    "expires_on": future_date,
                    "category": "legacy_compat",
                },
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file)
        assert result.has_errors()
        assert "same-id" in result.duplicate_ids
        assert any("重复" in e.message for e in result.errors)

    def test_invalid_owner_format_fails(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """无效的 owner 格式应导致校验失败。"""
        future_date = (date.today() + timedelta(days=30)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "bad-owner-entry",
                    "file_glob": "src/*.py",
                    "reason": "Test reason for entry",
                    "owner": "team-without-at",  # 缺少 @
                    "expires_on": future_date,
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file)
        assert result.has_errors()
        assert any("格式无效" in e.message for e in result.errors)

    def test_invalid_category_fails(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """无效的 category 应导致校验失败。"""
        future_date = (date.today() + timedelta(days=30)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "bad-category-entry",
                    "file_glob": "src/*.py",
                    "reason": "Test reason for entry",
                    "owner": "@team",
                    "expires_on": future_date,
                    "category": "invalid_category",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file)
        assert result.has_errors()
        assert any("category" in e.field for e in result.errors)


# ============================================================================
# Max Expiry 和 Expiring Soon 集成测试
# ============================================================================


class TestMaxExpiryIntegration:
    """测试 max_expiry_days 和 expiring_soon_days 集成场景。"""

    def test_exceeds_max_expiry_with_fail_on_max_expiry_fails(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """过期日超过 180 天且启用 fail_on_max_expiry 时应失败。

        验证规则：
        - 默认 max_expiry_days = 180
        - 当 fail_on_max_expiry=True 时，超出此期限的条目会产生 error 而非 warning
        - 结果的 has_errors() 应返回 True
        """
        # 设置一个距离今天 200 天的过期日期（超过 180 天限制）
        far_future_date = (date.today() + timedelta(days=200)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "far-future-entry",
                    "file_glob": "src/*.py",
                    "reason": "Test reason for far future expiry entry",
                    "owner": "@platform-team",
                    "expires_on": far_future_date,
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        # 启用 fail_on_max_expiry，使用默认的 180 天限制
        result = validate_allowlist(
            allowlist_file,
            schema_file,
            max_expiry_days=180,
            fail_on_max_expiry=True,
        )

        # 应该失败
        assert result.has_errors(), "超过 max_expiry_days 且启用 fail_on_max_expiry 时应失败"
        # 应该记录在 exceeds_max_expiry_entries 中
        assert len(result.exceeds_max_expiry_entries) == 1
        assert result.exceeds_max_expiry_entries[0].entry_id == "far-future-entry"
        # 应该有对应的 error
        assert any(
            "超过最大期限" in e.message and e.entry_id == "far-future-entry" for e in result.errors
        )

    def test_expiring_soon_produces_warning_but_passes(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """即将过期（<=14 天）的条目应产生 warning 但校验仍通过。

        验证规则：
        - 默认 expiring_soon_days = 14
        - 在 14 天内过期的条目会产生 warning
        - 但校验应通过（has_errors() 返回 False）
        """
        # 使用固定日期避免 UTC 与本地时区差异
        fixed_today = date(2026, 2, 2)
        # 设置一个距离今天 10 天的过期日期（在 14 天预警期内）
        soon_date = (fixed_today + timedelta(days=10)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "expiring-soon-entry",
                    "file_glob": "src/*.py",
                    "reason": "Test reason for expiring soon entry",
                    "owner": "@platform-team",
                    "expires_on": soon_date,
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        # 使用默认的 14 天预警期
        result = validate_allowlist(
            allowlist_file,
            schema_file,
            today=fixed_today,
            expiring_soon_days=14,
        )

        # 校验应通过（无 error）
        assert not result.has_errors(), "即将过期的条目不应导致校验失败"
        # 应该有 warning
        assert result.has_warnings(), "即将过期的条目应产生 warning"
        # 应该记录在 expiring_soon_entries 中
        assert len(result.expiring_soon_entries) == 1
        assert result.expiring_soon_entries[0].entry_id == "expiring-soon-entry"
        assert result.expiring_soon_entries[0].days_until_expiry == 10
        # 确认 warning 消息内容
        assert any(
            "即将过期" in w.message and w.entry_id == "expiring-soon-entry" for w in result.warnings
        )


class TestExceedsMaxExpiryWithTodayInjection:
    """测试 exceeds_max_expiry 功能 - 使用固定 today 日期注入。

    验证规则：
    - 当 expires_on 距离 today > max_expiry_days 时，记录到 exceeds_max_expiry_entries
    - warn-only 模式（默认）：ok=true，warnings 含 exceeds_max_expiry
    - fail-on-max-expiry 模式：ok=false，errors 含 exceeds_max_expiry
    """

    def test_exceeds_max_expiry_warn_only_mode_ok_true_with_warning(
        self, tmp_path: Path, minimal_schema: dict
    ) -> None:
        """warn-only 模式下 ok=true 且 warnings 含 exceeds_max_expiry。

        构造场景：
        - 固定 today = 2026-01-01
        - expires_on = 2026-08-01 (距离 today 212 天 > 180 天)
        - fail_on_max_expiry = False（默认）

        预期：
        - ok=true（无 error）
        - exceeds_max_expiry_entries 非空
        - warnings 含 "超过" 相关消息
        """
        fixed_today = date(2026, 1, 1)
        # 设置一个超过 180 天的过期日期（212 天）
        expires_date = date(2026, 8, 1)

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "test-exceeds-max",
                    "file_glob": "src/*.py",
                    "reason": "测试超过最大期限的条目",
                    "owner": "@platform-team",
                    "expires_on": expires_date.isoformat(),
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        # 使用 today 参数注入固定日期
        result = validate_allowlist(
            allowlist_file,
            schema_file,
            today=fixed_today,
            max_expiry_days=180,
            fail_on_max_expiry=False,  # warn-only 模式
        )

        # 验证 ok=true（无 error）
        assert not result.has_errors(), "warn-only 模式下 ok 应为 true"

        # 验证 exceeds_max_expiry_entries 非空
        assert len(result.exceeds_max_expiry_entries) == 1
        assert result.exceeds_max_expiry_entries[0].entry_id == "test-exceeds-max"
        # 验证 days_until_expiry 计算正确
        expected_days = (expires_date - fixed_today).days
        assert result.exceeds_max_expiry_entries[0].days_until_expiry == expected_days
        assert expected_days > 180  # 确认确实超过 180 天

        # 验证 warnings 含 exceeds_max_expiry 相关消息
        exceeds_warnings = [
            w for w in result.warnings if "超过" in w.message or "期限" in w.message
        ]
        assert len(exceeds_warnings) >= 1

    def test_exceeds_max_expiry_fail_on_max_expiry_mode_ok_false_with_error(
        self, tmp_path: Path, minimal_schema: dict
    ) -> None:
        """fail-on-max-expiry 模式下 ok=false 且 errors 含 exceeds_max_expiry。

        构造场景：
        - 固定 today = 2026-01-01
        - expires_on = 2026-08-01 (距离 today 212 天 > 180 天)
        - fail_on_max_expiry = True

        预期：
        - ok=false（有 error）
        - exceeds_max_expiry_entries 非空
        - errors 含 "超过最大期限" 相关消息
        """
        fixed_today = date(2026, 1, 1)
        # 设置一个超过 180 天的过期日期（212 天）
        expires_date = date(2026, 8, 1)

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "test-exceeds-max-fail",
                    "file_glob": "src/*.py",
                    "reason": "测试超过最大期限的条目（fail 模式）",
                    "owner": "@platform-team",
                    "expires_on": expires_date.isoformat(),
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        # 使用 today 参数注入固定日期
        result = validate_allowlist(
            allowlist_file,
            schema_file,
            today=fixed_today,
            max_expiry_days=180,
            fail_on_max_expiry=True,  # fail 模式
        )

        # 验证 ok=false（有 error）
        assert result.has_errors(), "fail-on-max-expiry 模式下 ok 应为 false"

        # 验证 exceeds_max_expiry_entries 非空
        assert len(result.exceeds_max_expiry_entries) == 1
        assert result.exceeds_max_expiry_entries[0].entry_id == "test-exceeds-max-fail"

        # 验证 errors 含 "超过最大期限" 相关消息
        exceeds_errors = [e for e in result.errors if "超过最大期限" in e.message]
        assert len(exceeds_errors) >= 1
        assert exceeds_errors[0].entry_id == "test-exceeds-max-fail"

    def test_within_max_expiry_no_warning_no_error(
        self, tmp_path: Path, minimal_schema: dict
    ) -> None:
        """在 max_expiry_days 内的条目不应产生 exceeds_max_expiry 警告或错误。

        构造场景：
        - 固定 today = 2026-01-01
        - expires_on = 2026-04-01 (距离 today 90 天 < 180 天)

        预期：
        - ok=true
        - exceeds_max_expiry_entries 为空
        """
        fixed_today = date(2026, 1, 1)
        # 设置一个在 180 天内的过期日期（90 天）
        expires_date = date(2026, 4, 1)

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "test-within-max",
                    "file_glob": "src/*.py",
                    "reason": "测试在最大期限内的条目",
                    "owner": "@platform-team",
                    "expires_on": expires_date.isoformat(),
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(
            allowlist_file,
            schema_file,
            today=fixed_today,
            max_expiry_days=180,
            fail_on_max_expiry=True,  # 即使启用 fail 模式也不应有问题
        )

        # 验证 ok=true
        assert not result.has_errors()
        # 验证 exceeds_max_expiry_entries 为空
        assert len(result.exceeds_max_expiry_entries) == 0

    def test_boundary_exactly_max_expiry_days_no_exceed(
        self, tmp_path: Path, minimal_schema: dict
    ) -> None:
        """边界条件：正好等于 max_expiry_days 不应视为超过。

        构造场景：
        - 固定 today = 2026-01-01
        - expires_on = 2026-06-30 (距离 today 正好 180 天)

        预期：
        - exceeds_max_expiry_entries 为空（180 天边界不算超过）
        """
        fixed_today = date(2026, 1, 1)
        # 正好 180 天后
        expires_date = fixed_today + timedelta(days=180)

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "test-boundary-180",
                    "file_glob": "src/*.py",
                    "reason": "测试边界 180 天的条目",
                    "owner": "@platform-team",
                    "expires_on": expires_date.isoformat(),
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(
            allowlist_file,
            schema_file,
            today=fixed_today,
            max_expiry_days=180,
        )

        # 验证 exceeds_max_expiry_entries 为空（180 天边界不算超过）
        assert len(result.exceeds_max_expiry_entries) == 0

    def test_boundary_max_expiry_days_plus_one_exceeds(
        self, tmp_path: Path, minimal_schema: dict
    ) -> None:
        """边界条件：max_expiry_days + 1 应视为超过。

        构造场景：
        - 固定 today = 2026-01-01
        - expires_on = 2026-07-01 (距离 today 181 天 > 180 天)

        预期：
        - exceeds_max_expiry_entries 非空
        """
        fixed_today = date(2026, 1, 1)
        # 181 天后
        expires_date = fixed_today + timedelta(days=181)

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "test-boundary-181",
                    "file_glob": "src/*.py",
                    "reason": "测试边界 181 天的条目",
                    "owner": "@platform-team",
                    "expires_on": expires_date.isoformat(),
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(
            allowlist_file,
            schema_file,
            today=fixed_today,
            max_expiry_days=180,
        )

        # 验证 exceeds_max_expiry_entries 非空
        assert len(result.exceeds_max_expiry_entries) == 1
        assert result.exceeds_max_expiry_entries[0].days_until_expiry == 181

    def test_json_output_contains_exceeds_max_expiry_in_warn_mode(
        self, tmp_path: Path, minimal_schema: dict
    ) -> None:
        """验证 JSON 输出在 warn-only 模式下包含 exceeds_max_expiry_entries 且 ok=true。"""
        from scripts.ci.check_gateway_deps_db_allowlist import main

        fixed_today = date(2026, 1, 1)
        expires_date = date(2026, 8, 1)  # 超过 180 天

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "json-test-exceeds",
                    "file_glob": "src/*.py",
                    "reason": "JSON 输出测试超过期限",
                    "owner": "@platform-team",
                    "expires_on": expires_date.isoformat(),
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch(
            "sys.argv",
            [
                "check_gateway_deps_db_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--json",
                "--max-expiry-days",
                "180",
                # 不传 --fail-on-max-expiry（warn-only 模式）
            ],
        ):
            with patch(
                "scripts.ci.check_gateway_deps_db_allowlist.utc_today", return_value=fixed_today
            ):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

        # warn-only 模式下 exit_code 应为 0
        assert exit_code == 0

        output = captured.getvalue()
        result = json.loads(output)

        # 验证 ok=true
        assert result["ok"] is True
        # 验证 exceeds_max_expiry_entries 非空
        assert len(result["exceeds_max_expiry_entries"]) == 1
        assert result["exceeds_max_expiry_entries"][0]["entry_id"] == "json-test-exceeds"

    def test_json_output_contains_exceeds_max_expiry_in_fail_mode(
        self, tmp_path: Path, minimal_schema: dict
    ) -> None:
        """验证 JSON 输出在 fail-on-max-expiry 模式下 ok=false 且 errors 非空。"""
        from scripts.ci.check_gateway_deps_db_allowlist import main

        fixed_today = date(2026, 1, 1)
        expires_date = date(2026, 8, 1)  # 超过 180 天

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "json-test-exceeds-fail",
                    "file_glob": "src/*.py",
                    "reason": "JSON 输出测试（fail 模式）",
                    "owner": "@platform-team",
                    "expires_on": expires_date.isoformat(),
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch(
            "sys.argv",
            [
                "check_gateway_deps_db_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--json",
                "--max-expiry-days",
                "180",
                "--fail-on-max-expiry",  # fail 模式
            ],
        ):
            with patch(
                "scripts.ci.check_gateway_deps_db_allowlist.utc_today", return_value=fixed_today
            ):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

        # fail-on-max-expiry 模式下 exit_code 应为 1
        assert exit_code == 1

        output = captured.getvalue()
        result = json.loads(output)

        # 验证 ok=false
        assert result["ok"] is False
        # 验证 exceeds_max_expiry_entries 非空
        assert len(result["exceeds_max_expiry_entries"]) == 1
        # 验证 errors 包含相关信息
        assert len(result["errors"]) >= 1
        error_messages = [e["message"] for e in result["errors"]]
        assert any("超过最大期限" in msg for msg in error_messages)


# ============================================================================
# ValidationResult 测试
# ============================================================================


class TestValidationResult:
    """测试 ValidationResult 数据类。"""

    def test_has_errors_with_errors(self) -> None:
        """有错误时 has_errors 应返回 True。"""
        result = ValidationResult()
        result.errors.append(ValidationError(entry_id="test", field="owner", message="Missing"))
        assert result.has_errors()

    def test_has_errors_with_schema_errors(self) -> None:
        """有 schema 错误时 has_errors 应返回 True。"""
        result = ValidationResult()
        result.schema_errors.append("Invalid version")
        assert result.has_errors()

    def test_has_errors_without_errors(self) -> None:
        """无错误时 has_errors 应返回 False。"""
        result = ValidationResult()
        result.warnings.append(
            ValidationError(
                entry_id="test", field="id", message="Format warning", severity="warning"
            )
        )
        assert not result.has_errors()

    def test_to_dict(self) -> None:
        """to_dict 应正确序列化。"""
        result = ValidationResult()
        result.entries_checked = 5
        result.errors.append(ValidationError(entry_id="test", field="owner", message="Missing"))
        result.duplicate_ids.append("dup-id")

        data = result.to_dict()

        assert data["ok"] is False
        assert data["entries_checked"] == 5
        assert len(data["errors"]) == 1
        assert data["errors"][0]["entry_id"] == "test"
        assert "dup-id" in data["duplicate_ids"]


# ============================================================================
# main() 函数集成测试
# ============================================================================


class TestMainFunction:
    """测试 main() 函数的退出码和输出。"""

    def test_valid_allowlist_returns_zero(
        self,
        tmp_path: Path,
        valid_allowlist_data: dict,
        minimal_schema: dict,
    ) -> None:
        """有效 allowlist 应返回 0。"""
        from scripts.ci.check_gateway_deps_db_allowlist import main

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(valid_allowlist_data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch(
            "sys.argv",
            [
                "check_gateway_deps_db_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 0
        output = captured.getvalue()
        assert "[OK]" in output

    def test_invalid_allowlist_returns_one(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """无效 allowlist 应返回 1。"""
        from scripts.ci.check_gateway_deps_db_allowlist import main

        # 缺少必需字段的 allowlist
        invalid_data = {
            "version": "1",
            "entries": [{"id": "bad-entry"}],  # 缺少其他必需字段
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(invalid_data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch(
            "sys.argv",
            [
                "check_gateway_deps_db_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 1
        output = captured.getvalue()
        assert "[FAIL]" in output

    def test_json_output_format(
        self,
        tmp_path: Path,
        valid_allowlist_data: dict,
        minimal_schema: dict,
    ) -> None:
        """--json 应输出有效 JSON。"""
        from scripts.ci.check_gateway_deps_db_allowlist import main

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(valid_allowlist_data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch(
            "sys.argv",
            [
                "check_gateway_deps_db_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--json",
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                main()

        output = captured.getvalue()
        data = json.loads(output)  # 应能解析为 JSON
        assert "ok" in data
        assert "entries_checked" in data
        assert "duplicate_ids" in data
