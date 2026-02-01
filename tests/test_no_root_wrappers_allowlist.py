"""
tests/test_no_root_wrappers_allowlist.py

测试 scripts/ci/check_no_root_wrappers_allowlist.py 的核心功能:
1. JSON Schema 校验
2. expires_on 日期格式与过期校验
3. owner 字段校验
4. 条目使用情况检查
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

# 将 scripts/ci 加入 path 以便导入
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "ci"))

from check_no_root_wrappers_allowlist import (
    DATE_PATTERN,
    OWNER_PATTERN,
    ValidationError,
    ValidationResult,
    check_all_entries_usage,
    check_entry_usage,
    get_entry_required_fields,
    get_entry_valid_fields,
    load_json_file,
    validate_against_schema,
    validate_allowlist,
    validate_date_format,
    validate_entry_fields,
    validate_expires_on,
    validate_id_format,
    validate_owner,
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
                "id": "test-entry-1",
                "file_pattern": "tests/**/*.py",
                "module": "db",
                "owner": "@engram-team",
                "expiry": future_date,
                "reason": "测试用例需要验证根目录 wrapper 的行为",
            }
        ],
    }


@pytest.fixture
def minimal_schema() -> dict:
    """最小 schema 定义。"""
    return {
        "definitions": {
            "allowlist_entry": {
                "required": ["id", "module", "owner", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "scope": {"type": "string"},
                    "module": {"type": "string"},
                    "file_glob": {"type": "string"},
                    "file_path": {"type": "string"},
                    "reason": {"type": "string"},
                    "owner": {"type": "string"},
                    "expires_on": {"type": "string"},
                    "category": {"type": "string"},
                    "created_at": {"type": "string"},
                    "jira_ticket": {"type": "string"},
                    "notes": {"type": "string"},
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

    def test_valid_owner_without_at_sign(self) -> None:
        """不带 @ 的用户名应通过。"""
        assert OWNER_PATTERN.match("engram-team")
        assert OWNER_PATTERN.match("user123")

    def test_invalid_owner_with_spaces(self) -> None:
        """包含空格的应失败。"""
        assert not OWNER_PATTERN.match("@engram team")
        assert not OWNER_PATTERN.match("user name")

    def test_invalid_owner_special_chars(self) -> None:
        """包含特殊字符的应失败。"""
        assert not OWNER_PATTERN.match("@user@team")
        assert not OWNER_PATTERN.match("user.name")


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

    def test_non_standard_owner_format_warns(self) -> None:
        """非标准格式的 owner 应产生警告。"""
        result = ValidationResult()
        entry = {"owner": "some.user@company.com"}  # email 格式
        validate_owner(entry, "test-id", result)
        # 应有警告但无错误
        assert len(result.errors) == 0
        assert len(result.warnings) == 1


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
        past_date = (date.today() - timedelta(days=1)).isoformat()
        entry = {"expires_on": past_date}
        assert not validate_expires_on(entry, "test-id", result)
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

    def test_expiry_field_alias(self) -> None:
        """expiry 字段别名应被支持。"""
        result = ValidationResult()
        future_date = (date.today() + timedelta(days=30)).isoformat()
        entry = {"expiry": future_date}
        assert validate_expires_on(entry, "test-id", result)
        assert len(result.errors) == 0

    def test_missing_expires_on_passes(self) -> None:
        """缺少 expires_on 应通过（可选字段）。"""
        result = ValidationResult()
        entry = {}
        assert validate_expires_on(entry, "test-id", result)
        assert len(result.errors) == 0


class TestValidateExpiresOnWithTodayInjection:
    """测试 validate_expires_on 函数 - today 参数注入边界测试。"""

    def test_today_equals_expires_passes(self) -> None:
        """today == expires 应通过（边界条件）。"""
        result = ValidationResult()
        expires_date = date(2026, 6, 15)
        entry = {"expires_on": expires_date.isoformat()}

        # today == expires 应仍有效
        assert validate_expires_on(entry, "test-id", result, today=expires_date)
        assert len(result.errors) == 0
        assert len(result.expired_entries) == 0

    def test_today_greater_than_expires_fails(self) -> None:
        """today > expires 应失败。"""
        result = ValidationResult()
        expires_date = date(2026, 6, 15)
        entry = {"expires_on": expires_date.isoformat()}

        # today > expires 应过期
        assert not validate_expires_on(
            entry, "test-id", result, today=expires_date + timedelta(days=1)
        )
        assert "test-id" in result.expired_entries

    def test_today_less_than_expires_passes(self) -> None:
        """today < expires 应通过。"""
        result = ValidationResult()
        expires_date = date(2026, 6, 15)
        entry = {"expires_on": expires_date.isoformat()}

        # today < expires 应仍有效
        assert validate_expires_on(entry, "test-id", result, today=expires_date - timedelta(days=1))
        assert len(result.errors) == 0
        assert len(result.expired_entries) == 0

    def test_expiring_soon_with_injected_today(self) -> None:
        """使用注入的 today 测试即将过期警告。"""
        result = ValidationResult()
        # 设置过期日期为 today + 7 天（在 14 天预警期内）
        fixed_today = date(2026, 6, 1)
        expires_date = fixed_today + timedelta(days=7)
        entry = {"expires_on": expires_date.isoformat(), "owner": "@team", "category": "test"}

        # 应通过但产生警告
        assert validate_expires_on(entry, "test-id", result, today=fixed_today)
        assert len(result.errors) == 0
        assert len(result.expiring_soon_entries) == 1
        assert result.expiring_soon_entries[0].days_until_expiry == 7


class TestValidateExpiresOnMaxExpiry:
    """测试 validate_expires_on 函数 - 最大期限检测。"""

    def test_exceeds_max_expiry_default_warn(self) -> None:
        """超过最大期限（180天）默认产生警告。"""
        result = ValidationResult()
        fixed_today = date(2026, 6, 1)
        # 设置过期日期为 today + 200 天（超过默认 180 天最大期限）
        expires_date = fixed_today + timedelta(days=200)
        entry = {"expires_on": expires_date.isoformat(), "owner": "@team", "category": "test"}

        # 应通过但产生警告
        assert validate_expires_on(entry, "test-id", result, today=fixed_today)
        assert len(result.errors) == 0
        assert len(result.exceeds_max_expiry_entries) == 1
        assert result.exceeds_max_expiry_entries[0].days_until_expiry == 200

    def test_exceeds_max_expiry_fail_on_max_expiry(self) -> None:
        """启用 fail_on_max_expiry 时超过最大期限应报错。"""
        result = ValidationResult()
        fixed_today = date(2026, 6, 1)
        # 设置过期日期为 today + 200 天
        expires_date = fixed_today + timedelta(days=200)
        entry = {"expires_on": expires_date.isoformat(), "owner": "@team", "category": "test"}

        # 启用 fail_on_max_expiry，应失败
        assert not validate_expires_on(
            entry, "test-id", result, today=fixed_today, fail_on_max_expiry=True
        )
        assert len(result.errors) == 1
        assert "超过最大期限" in result.errors[0].message

    def test_within_max_expiry_passes(self) -> None:
        """在最大期限内（180天）应通过无警告。"""
        result = ValidationResult()
        fixed_today = date(2026, 6, 1)
        # 设置过期日期为 today + 100 天（在 180 天内）
        expires_date = fixed_today + timedelta(days=100)
        entry = {"expires_on": expires_date.isoformat(), "owner": "@team", "category": "test"}

        # 应通过且无警告
        assert validate_expires_on(entry, "test-id", result, today=fixed_today)
        assert len(result.errors) == 0
        assert len(result.exceeds_max_expiry_entries) == 0

    def test_custom_max_expiry_days(self) -> None:
        """自定义最大期限天数。"""
        result = ValidationResult()
        fixed_today = date(2026, 6, 1)
        # 设置过期日期为 today + 100 天
        expires_date = fixed_today + timedelta(days=100)
        entry = {"expires_on": expires_date.isoformat(), "owner": "@team", "category": "test"}

        # 使用自定义 max_expiry_days=90，应产生警告
        assert validate_expires_on(entry, "test-id", result, today=fixed_today, max_expiry_days=90)
        assert len(result.exceeds_max_expiry_entries) == 1
        assert result.exceeds_max_expiry_entries[0].days_until_expiry == 100

    def test_boundary_exactly_max_expiry_passes(self) -> None:
        """正好等于最大期限边界应通过。"""
        result = ValidationResult()
        fixed_today = date(2026, 6, 1)
        # 设置过期日期正好为 today + 180 天（边界条件）
        expires_date = fixed_today + timedelta(days=180)
        entry = {"expires_on": expires_date.isoformat(), "owner": "@team", "category": "test"}

        # 正好等于最大期限边界，应通过
        assert validate_expires_on(entry, "test-id", result, today=fixed_today)
        assert len(result.exceeds_max_expiry_entries) == 0


class TestExpiringSoonAndMaxExpiryCombined:
    """测试 expiring_soon 和 max_expiry 同时存在的场景。"""

    def test_not_expiring_soon_but_exceeds_max(self) -> None:
        """不在即将过期范围但超过最大期限。"""
        result = ValidationResult()
        fixed_today = date(2026, 6, 1)
        # 设置过期日期为 today + 200 天（不在 14 天预警期，但超过 180 天）
        expires_date = fixed_today + timedelta(days=200)
        entry = {"expires_on": expires_date.isoformat(), "owner": "@team", "category": "test"}

        assert validate_expires_on(entry, "test-id", result, today=fixed_today)
        assert len(result.expiring_soon_entries) == 0  # 不在即将过期范围
        assert len(result.exceeds_max_expiry_entries) == 1  # 超过最大期限

    def test_within_all_thresholds(self) -> None:
        """在所有阈值范围内（正常有效期）。"""
        result = ValidationResult()
        fixed_today = date(2026, 6, 1)
        # 设置过期日期为 today + 90 天（既不即将过期，也不超过最大期限）
        expires_date = fixed_today + timedelta(days=90)
        entry = {"expires_on": expires_date.isoformat(), "owner": "@team", "category": "test"}

        assert validate_expires_on(entry, "test-id", result, today=fixed_today)
        assert len(result.expiring_soon_entries) == 0
        assert len(result.exceeds_max_expiry_entries) == 0


# ============================================================================
# ID 格式校验测试
# ============================================================================


class TestValidateIdFormat:
    """测试 validate_id_format 函数。"""

    def test_valid_id(self) -> None:
        """有效的 id 应通过。"""
        result = ValidationResult()
        entry = {"id": "import-db-test-1"}
        assert validate_id_format(entry, "import-db-test-1", result)
        assert len(result.warnings) == 0

    def test_empty_id_fails(self) -> None:
        """空 id 应报错。"""
        result = ValidationResult()
        entry = {"id": ""}
        assert not validate_id_format(entry, "", result)
        assert len(result.errors) == 1

    def test_uppercase_id_warns(self) -> None:
        """大写 id 应产生警告。"""
        result = ValidationResult()
        entry = {"id": "Import-DB-Test"}
        validate_id_format(entry, "Import-DB-Test", result)
        assert len(result.warnings) == 1
        assert "格式不规范" in result.warnings[0].message


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
            "module": "db",
            "owner": "@team",
            "reason": "Test reason",
            "file_pattern": "tests/*.py",
        }
        required = ["id", "module", "owner", "reason"]
        valid_fields = set(entry.keys())
        assert validate_entry_fields(entry, "test-1", required, valid_fields, result)
        assert len(result.errors) == 0

    def test_missing_required_field(self) -> None:
        """缺少必需字段应报错。"""
        result = ValidationResult()
        entry = {"id": "test-1", "module": "db"}
        required = ["id", "module", "owner", "reason"]
        valid_fields = {"id", "module", "owner", "reason"}
        assert not validate_entry_fields(entry, "test-1", required, valid_fields, result)
        assert len(result.errors) >= 1

    def test_file_glob_alias(self) -> None:
        """file_pattern 应作为 file_glob 的别名。"""
        result = ValidationResult()
        entry = {
            "id": "test-1",
            "file_pattern": "tests/*.py",
            "module": "db",
            "owner": "@team",
            "reason": "Test",
        }
        required = ["id", "file_glob", "module", "owner", "reason"]
        valid_fields = {"id", "file_glob", "module", "owner", "reason"}
        assert validate_entry_fields(entry, "test-1", required, valid_fields, result)
        assert len(result.errors) == 0

    def test_unknown_field_warns(self) -> None:
        """未知字段应产生警告。"""
        result = ValidationResult()
        entry = {
            "id": "test-1",
            "module": "db",
            "owner": "@team",
            "reason": "Test",
            "unknown_field": "value",
        }
        required = ["id", "module", "owner", "reason"]
        valid_fields = {"id", "module", "owner", "reason"}
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
        assert "module" in required
        assert "owner" in required
        assert "reason" in required

    def test_get_entry_valid_fields(self, minimal_schema: dict) -> None:
        """应正确提取有效字段。"""
        valid_fields = get_entry_valid_fields(minimal_schema)
        assert "id" in valid_fields
        assert "module" in valid_fields
        assert "expires_on" in valid_fields
        assert "notes" in valid_fields


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

        result = validate_allowlist(allowlist_file, schema_file, tmp_path, check_usage=False)
        assert not result.has_errors()
        assert result.entries_checked == 1

    def test_expired_entry_fails(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """过期条目应导致校验失败。"""
        past_date = (date.today() - timedelta(days=1)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "expired-entry",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expiry": past_date,
                    "reason": "Test reason for expired entry",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file, tmp_path, check_usage=False)
        assert result.has_errors()
        assert "expired-entry" in result.expired_entries

    def test_missing_owner_fails(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """缺少 owner 应导致校验失败。"""
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "no-owner-entry",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "reason": "Test reason",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file, tmp_path, check_usage=False)
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
                    "file_pattern": "tests/a.py",
                    "module": "db",
                    "owner": "@team",
                    "expiry": future_date,
                    "reason": "First entry",
                },
                {
                    "id": "same-id",
                    "file_pattern": "tests/b.py",
                    "module": "kv",
                    "owner": "@team",
                    "expiry": future_date,
                    "reason": "Second entry",
                },
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file, tmp_path, check_usage=False)
        assert result.has_errors()
        assert any("重复" in e.message for e in result.errors)


# ============================================================================
# 使用情况检查测试
# ============================================================================


class TestUsageCheck:
    """测试条目使用情况检查。"""

    def test_used_entry_detected(self, tmp_path: Path) -> None:
        """使用的条目应被检测到。"""
        # 创建测试文件结构
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        test_file = src_dir / "test.py"
        test_file.write_text("import db\n", encoding="utf-8")

        entry = {
            "id": "test-entry",
            "file_pattern": "src/*.py",
            "module": "db",
        }

        result = ValidationResult()
        used = check_entry_usage(entry, "test-entry", tmp_path, result)

        assert used
        assert len(result.usage_hits) == 1
        assert result.usage_hits[0].entry_id == "test-entry"

    def test_unused_entry_detected(self, tmp_path: Path) -> None:
        """未使用的条目应被检测到。"""
        # 创建不包含匹配导入的测试文件
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        test_file = src_dir / "test.py"
        test_file.write_text("import other_module\n", encoding="utf-8")

        entry = {
            "id": "unused-entry",
            "file_pattern": "src/*.py",
            "module": "db",
        }

        result = ValidationResult()
        used = check_entry_usage(entry, "unused-entry", tmp_path, result)

        assert not used
        assert len(result.usage_hits) == 0

    def test_strict_mode_unused_as_error(self, tmp_path: Path) -> None:
        """严格模式下未使用条目应报错。"""
        # 创建空的 src 目录
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        test_file = src_dir / "test.py"
        test_file.write_text("# empty file\n", encoding="utf-8")

        future_date = (date.today() + timedelta(days=30)).isoformat()
        entries = [
            {
                "id": "unused-entry",
                "file_pattern": "src/*.py",
                "module": "nonexistent_module",
                "owner": "@team",
                "expiry": future_date,
                "reason": "Test reason",
            }
        ]

        result = ValidationResult()
        check_all_entries_usage(entries, tmp_path, result, strict=True)

        assert "unused-entry" in result.unused_entries
        assert any(e.entry_id == "unused-entry" for e in result.errors)

    def test_non_strict_mode_unused_as_warning(self, tmp_path: Path) -> None:
        """非严格模式下未使用条目应为警告。"""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        test_file = src_dir / "test.py"
        test_file.write_text("# empty file\n", encoding="utf-8")

        future_date = (date.today() + timedelta(days=30)).isoformat()
        entries = [
            {
                "id": "unused-entry",
                "file_pattern": "src/*.py",
                "module": "nonexistent_module",
                "owner": "@team",
                "expiry": future_date,
                "reason": "Test reason",
            }
        ]

        result = ValidationResult()
        check_all_entries_usage(entries, tmp_path, result, strict=False)

        assert "unused-entry" in result.unused_entries
        assert any(w.entry_id == "unused-entry" for w in result.warnings)
        assert not any(e.entry_id == "unused-entry" for e in result.errors)


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

        data = result.to_dict()

        assert data["ok"] is False
        assert data["entries_checked"] == 5
        assert len(data["errors"]) == 1
        assert data["errors"][0]["entry_id"] == "test"


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
        from check_no_root_wrappers_allowlist import main

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(valid_allowlist_data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch(
            "sys.argv",
            [
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
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
        from check_no_root_wrappers_allowlist import main

        # 缺少必需字段的 allowlist
        invalid_data = {
            "version": "1",
            "entries": [{"id": "bad-entry"}],  # 缺少 owner, module, reason
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(invalid_data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch(
            "sys.argv",
            [
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
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
        from check_no_root_wrappers_allowlist import main

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(valid_allowlist_data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch(
            "sys.argv",
            [
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
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


# ============================================================================
# Category/Owner 汇总测试
# ============================================================================


# ============================================================================
# --fail-on-max-expiry CLI 退出码测试
# ============================================================================


class TestFailOnMaxExpiryExitCode:
    """测试 --fail-on-max-expiry 参数对 exit code 的影响。"""

    def test_exceeds_max_expiry_with_fail_flag_returns_one(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """超过 max-expiry 且启用 --fail-on-max-expiry 时应返回 exit code=1。"""
        from check_no_root_wrappers_allowlist import main

        # 设置一个超过 180 天的过期日期
        far_future_date = (date.today() + timedelta(days=365)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "far-future-entry",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": far_future_date,
                    "reason": "Test reason for far future entry",
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
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
                "--fail-on-max-expiry",
                "--max-expiry-days",
                "180",
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 1
        output = captured.getvalue()
        assert "[FAIL]" in output or "[ERROR]" in output

    def test_exceeds_max_expiry_without_fail_flag_returns_zero(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """超过 max-expiry 但未启用 --fail-on-max-expiry 时应返回 exit code=0（仅警告）。"""
        from check_no_root_wrappers_allowlist import main

        # 设置一个超过 180 天的过期日期
        far_future_date = (date.today() + timedelta(days=365)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "far-future-entry",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": far_future_date,
                    "reason": "Test reason for far future entry",
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
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
                # 不传 --fail-on-max-expiry
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 0
        output = captured.getvalue()
        assert "[OK]" in output

    def test_expiring_soon_warning_returns_zero(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """即将过期（14天内）产生 warning 但 exit code 仍为 0。"""
        from check_no_root_wrappers_allowlist import main

        # 设置一个 7 天后过期的日期（在 14 天预警期内）
        soon_expiring_date = (date.today() + timedelta(days=7)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "soon-expiring-entry",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": soon_expiring_date,
                    "reason": "Test reason for soon expiring entry",
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
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
                "--fail-on-max-expiry",  # 即使启用也不影响即将过期的警告
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        # 即将过期只是警告，不影响 exit code
        assert exit_code == 0
        output = captured.getvalue()
        assert "[OK]" in output
        assert "[WARN]" in output or "即将过期" in output

    def test_expiring_soon_warning_in_json_output(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """验证 JSON 输出包含即将过期的警告信息。"""
        from check_no_root_wrappers_allowlist import main

        soon_expiring_date = (date.today() + timedelta(days=7)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "soon-expiring-entry",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": soon_expiring_date,
                    "reason": "Test reason",
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
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
                "--json",
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 0
        output = captured.getvalue()
        result = json.loads(output)

        assert result["ok"] is True
        assert len(result["expiring_soon_entries"]) == 1
        assert result["expiring_soon_entries"][0]["entry_id"] == "soon-expiring-entry"

    def test_exceeds_max_expiry_error_in_json_output(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """验证 --fail-on-max-expiry 时 JSON 输出包含错误信息。"""
        from check_no_root_wrappers_allowlist import main

        far_future_date = (date.today() + timedelta(days=365)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "far-future-entry",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": far_future_date,
                    "reason": "Test reason",
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
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
                "--fail-on-max-expiry",
                "--max-expiry-days",
                "180",
                "--json",
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 1
        output = captured.getvalue()
        result = json.loads(output)

        assert result["ok"] is False
        assert len(result["exceeds_max_expiry_entries"]) == 1
        assert len(result["errors"]) >= 1
        # 验证错误信息包含关键词
        error_messages = [e["message"] for e in result["errors"]]
        assert any("超过最大期限" in msg for msg in error_messages)


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
        expires_date = date(2026, 8, 1)  # 2026-01-01 + 212 days = 2026-08-01

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "test-exceeds-max",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": expires_date.isoformat(),
                    "reason": "测试超过最大期限的条目",
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        # 使用 monkeypatch 注入固定日期
        with patch("check_no_root_wrappers_allowlist.utc_today", return_value=fixed_today):
            result = validate_allowlist(
                allowlist_file,
                schema_file,
                tmp_path,
                check_usage=False,
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
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": expires_date.isoformat(),
                    "reason": "测试超过最大期限的条目（fail 模式）",
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        # 使用 monkeypatch 注入固定日期
        with patch("check_no_root_wrappers_allowlist.utc_today", return_value=fixed_today):
            result = validate_allowlist(
                allowlist_file,
                schema_file,
                tmp_path,
                check_usage=False,
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
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": expires_date.isoformat(),
                    "reason": "测试在最大期限内的条目",
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch("check_no_root_wrappers_allowlist.utc_today", return_value=fixed_today):
            result = validate_allowlist(
                allowlist_file,
                schema_file,
                tmp_path,
                check_usage=False,
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
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": expires_date.isoformat(),
                    "reason": "测试边界 180 天的条目",
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch("check_no_root_wrappers_allowlist.utc_today", return_value=fixed_today):
            result = validate_allowlist(
                allowlist_file,
                schema_file,
                tmp_path,
                check_usage=False,
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
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": expires_date.isoformat(),
                    "reason": "测试边界 181 天的条目",
                    "category": "testing",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch("check_no_root_wrappers_allowlist.utc_today", return_value=fixed_today):
            result = validate_allowlist(
                allowlist_file,
                schema_file,
                tmp_path,
                check_usage=False,
                max_expiry_days=180,
            )

        # 验证 exceeds_max_expiry_entries 非空
        assert len(result.exceeds_max_expiry_entries) == 1
        assert result.exceeds_max_expiry_entries[0].days_until_expiry == 181

    def test_json_output_contains_exceeds_max_expiry_in_warn_mode(
        self, tmp_path: Path, minimal_schema: dict
    ) -> None:
        """验证 JSON 输出在 warn-only 模式下包含 exceeds_max_expiry_entries 且 ok=true。"""
        from check_no_root_wrappers_allowlist import main

        fixed_today = date(2026, 1, 1)
        expires_date = date(2026, 8, 1)  # 超过 180 天

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "json-test-exceeds",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": expires_date.isoformat(),
                    "reason": "JSON 输出测试",
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
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
                "--json",
                "--max-expiry-days",
                "180",
                # 不传 --fail-on-max-expiry（warn-only 模式）
            ],
        ):
            with patch("check_no_root_wrappers_allowlist.utc_today", return_value=fixed_today):
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
        from check_no_root_wrappers_allowlist import main

        fixed_today = date(2026, 1, 1)
        expires_date = date(2026, 8, 1)  # 超过 180 天

        data = {
            "version": "1",
            "entries": [
                {
                    "id": "json-test-exceeds-fail",
                    "file_pattern": "tests/*.py",
                    "module": "db",
                    "owner": "@team",
                    "expires_on": expires_date.isoformat(),
                    "reason": "JSON 输出测试（fail 模式）",
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
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
                "--json",
                "--max-expiry-days",
                "180",
                "--fail-on-max-expiry",  # fail 模式
            ],
        ):
            with patch("check_no_root_wrappers_allowlist.utc_today", return_value=fixed_today):
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


class TestCategoryOwnerSummary:
    """测试 category 和 owner 汇总功能。"""

    def test_category_summary_generated(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """验证 category 汇总正确生成。"""
        future_date = (date.today() + timedelta(days=30)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "entry-1",
                    "file_pattern": "tests/a.py",
                    "module": "db",
                    "owner": "@team-a",
                    "expiry": future_date,
                    "reason": "Test reason 1",
                    "category": "testing",
                },
                {
                    "id": "entry-2",
                    "file_pattern": "tests/b.py",
                    "module": "kv",
                    "owner": "@team-b",
                    "expiry": future_date,
                    "reason": "Test reason 2",
                    "category": "testing",
                },
                {
                    "id": "entry-3",
                    "file_pattern": "src/c.py",
                    "module": "db",
                    "owner": "@team-a",
                    "expiry": future_date,
                    "reason": "Test reason 3",
                    "category": "migration",
                },
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file, tmp_path, check_usage=False)

        # 验证 category 汇总
        assert "testing" in result.category_summary
        assert "migration" in result.category_summary
        assert result.category_summary["testing"] == 2
        assert result.category_summary["migration"] == 1

    def test_owner_summary_generated(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """验证 owner 汇总正确生成。"""
        future_date = (date.today() + timedelta(days=30)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "entry-1",
                    "file_pattern": "tests/a.py",
                    "module": "db",
                    "owner": "@platform-team",
                    "expiry": future_date,
                    "reason": "Test reason 1",
                    "category": "testing",
                },
                {
                    "id": "entry-2",
                    "file_pattern": "tests/b.py",
                    "module": "kv",
                    "owner": "@platform-team",
                    "expiry": future_date,
                    "reason": "Test reason 2",
                    "category": "testing",
                },
                {
                    "id": "entry-3",
                    "file_pattern": "src/c.py",
                    "module": "db",
                    "owner": "@infra-team",
                    "expiry": future_date,
                    "reason": "Test reason 3",
                    "category": "migration",
                },
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        result = validate_allowlist(allowlist_file, schema_file, tmp_path, check_usage=False)

        # 验证 owner 汇总
        assert "@platform-team" in result.owner_summary
        assert "@infra-team" in result.owner_summary
        assert result.owner_summary["@platform-team"] == 2
        assert result.owner_summary["@infra-team"] == 1

    def test_summary_in_json_output(
        self,
        tmp_path: Path,
        minimal_schema: dict,
    ) -> None:
        """验证 JSON 输出包含汇总信息。"""
        from check_no_root_wrappers_allowlist import main

        future_date = (date.today() + timedelta(days=30)).isoformat()
        data = {
            "version": "1",
            "entries": [
                {
                    "id": "entry-1",
                    "file_pattern": "tests/a.py",
                    "module": "db",
                    "owner": "@team",
                    "expiry": future_date,
                    "reason": "Test reason",
                    "category": "testing",
                },
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        schema_file = tmp_path / "schema.json"

        allowlist_file.write_text(json.dumps(data), encoding="utf-8")
        schema_file.write_text(json.dumps(minimal_schema), encoding="utf-8")

        with patch(
            "sys.argv",
            [
                "check_no_root_wrappers_allowlist.py",
                "--allowlist-file",
                str(allowlist_file),
                "--schema-file",
                str(schema_file),
                "--project-root",
                str(tmp_path),
                "--skip-usage-check",
                "--json",
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                main()

        output = captured.getvalue()
        data = json.loads(output)

        assert "category_summary" in data
        assert "owner_summary" in data
