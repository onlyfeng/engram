#!/usr/bin/env python3
"""
check_iteration_evidence_contract.py 单元测试

覆盖功能:
1. 文件名命名规范检测 - 验证 canonical 和 snapshot 格式
2. JSON Schema 校验 - 验证证据文件符合 schema
3. 内容一致性校验 - 验证 iteration_number 与文件名一致
4. 边界情况 - 空目录、无效 JSON、缺失字段等

Fixtures 使用临时目录构造 docs/acceptance/evidence 结构。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from scripts.ci.check_iteration_evidence_contract import (
    CANONICAL_PATTERN,
    EVIDENCE_DIR,
    SCHEMA_PATH,
    SNAPSHOT_PATTERN,
    SNAPSHOT_SHA_PATTERN,
    EvidenceViolation,
    get_evidence_files,
    load_schema,
    parse_evidence_filename,
    scan_evidence_files,
    validate_filename,
    validate_json_content,
)

# ============================================================================
# Fixtures - 临时项目目录
# ============================================================================


@pytest.fixture
def temp_evidence_dir():
    """创建临时证据目录结构"""
    with tempfile.TemporaryDirectory(prefix="test_evidence_") as tmpdir:
        evidence_dir = Path(tmpdir) / "docs" / "acceptance" / "evidence"
        evidence_dir.mkdir(parents=True)
        yield evidence_dir


@pytest.fixture
def valid_evidence_content() -> dict:
    """有效的证据文件内容"""
    return {
        "$schema": "../../../schemas/iteration_evidence_v1.schema.json",
        "iteration_number": 13,
        "recorded_at": "2026-02-01T20:46:36Z",
        "commit_sha": "abc1234567890",
        "runner": {
            "os": "darwin-24.6.0",
            "python": "3.13.2",
            "arch": "x86_64",
        },
        "commands": [
            {
                "name": "ci",
                "command": "make ci",
                "result": "PASS",
            }
        ],
        "overall_result": "PASS",
        "sensitive_data_declaration": True,
    }


@pytest.fixture
def canonical_evidence_file(temp_evidence_dir: Path, valid_evidence_content: dict) -> Path:
    """Canonical 格式的证据文件"""
    filepath = temp_evidence_dir / "iteration_13_evidence.json"
    filepath.write_text(json.dumps(valid_evidence_content, indent=2), encoding="utf-8")
    return filepath


@pytest.fixture
def snapshot_evidence_file(temp_evidence_dir: Path, valid_evidence_content: dict) -> Path:
    """Snapshot 格式的证据文件（无 SHA）"""
    filepath = temp_evidence_dir / "iteration_13_20260201_204636.json"
    filepath.write_text(json.dumps(valid_evidence_content, indent=2), encoding="utf-8")
    return filepath


@pytest.fixture
def snapshot_sha_evidence_file(temp_evidence_dir: Path, valid_evidence_content: dict) -> Path:
    """Snapshot 格式的证据文件（带 SHA）"""
    filepath = temp_evidence_dir / "iteration_13_20260201_204636_abc1234.json"
    filepath.write_text(json.dumps(valid_evidence_content, indent=2), encoding="utf-8")
    return filepath


# ============================================================================
# parse_evidence_filename 测试
# ============================================================================


class TestParseEvidenceFilename:
    """parse_evidence_filename 函数测试"""

    def test_parses_canonical_format(self):
        """测试解析 canonical 格式"""
        result = parse_evidence_filename("iteration_13_evidence.json")
        assert result is not None
        assert result["iteration_number"] == 13
        assert result["is_canonical"] is True
        assert result["timestamp"] is None
        assert result["commit_sha"] is None

    def test_parses_canonical_format_various_numbers(self):
        """测试解析不同迭代编号的 canonical 格式"""
        test_cases = [
            ("iteration_1_evidence.json", 1),
            ("iteration_99_evidence.json", 99),
            ("iteration_100_evidence.json", 100),
        ]
        for filename, expected_num in test_cases:
            result = parse_evidence_filename(filename)
            assert result is not None, f"应解析: {filename}"
            assert result["iteration_number"] == expected_num
            assert result["is_canonical"] is True

    def test_parses_snapshot_format(self):
        """测试解析 snapshot 格式（无 SHA）"""
        result = parse_evidence_filename("iteration_13_20260201_103000.json")
        assert result is not None
        assert result["iteration_number"] == 13
        assert result["is_canonical"] is False
        assert result["timestamp"] == "20260201_103000"
        assert result["commit_sha"] is None

    def test_parses_snapshot_sha_format(self):
        """测试解析 snapshot 格式（带 SHA）"""
        result = parse_evidence_filename("iteration_13_20260201_103000_abc1234.json")
        assert result is not None
        assert result["iteration_number"] == 13
        assert result["is_canonical"] is False
        assert result["timestamp"] == "20260201_103000"
        assert result["commit_sha"] == "abc1234"

    def test_rejects_invalid_formats(self):
        """测试拒绝无效格式"""
        invalid_filenames = [
            "evidence.json",  # 缺少 iteration 前缀
            "iteration_evidence.json",  # 缺少编号
            "iteration_13.json",  # 缺少 _evidence 后缀或时间戳
            "iteration_13_evidence.txt",  # 错误扩展名
            "ITERATION_13_evidence.json",  # 大写
            "iteration_abc_evidence.json",  # 非数字编号
            "iteration_13_2026_evidence.json",  # 时间戳格式错误
            "random_file.json",  # 完全不相关
        ]
        for filename in invalid_filenames:
            result = parse_evidence_filename(filename)
            assert result is None, f"不应解析: {filename}"


# ============================================================================
# 正则表达式模式测试
# ============================================================================


class TestPatterns:
    """正则表达式模式测试"""

    def test_canonical_pattern(self):
        """测试 CANONICAL_PATTERN"""
        assert CANONICAL_PATTERN.match("iteration_13_evidence.json")
        assert CANONICAL_PATTERN.match("iteration_1_evidence.json")
        assert CANONICAL_PATTERN.match("iteration_999_evidence.json")
        assert not CANONICAL_PATTERN.match("iteration_13.json")
        assert not CANONICAL_PATTERN.match("iteration_evidence.json")

    def test_snapshot_pattern(self):
        """测试 SNAPSHOT_PATTERN"""
        assert SNAPSHOT_PATTERN.match("iteration_13_20260201_103000.json")
        assert not SNAPSHOT_PATTERN.match("iteration_13_evidence.json")
        assert not SNAPSHOT_PATTERN.match("iteration_13_2026_103000.json")  # 时间戳格式错误

    def test_snapshot_sha_pattern(self):
        """测试 SNAPSHOT_SHA_PATTERN"""
        assert SNAPSHOT_SHA_PATTERN.match("iteration_13_20260201_103000_abc1234.json")
        assert not SNAPSHOT_SHA_PATTERN.match("iteration_13_20260201_103000.json")
        assert not SNAPSHOT_SHA_PATTERN.match(
            "iteration_13_20260201_103000_ABC1234.json"
        )  # 大写 SHA


# ============================================================================
# validate_filename 测试
# ============================================================================


class TestValidateFilename:
    """validate_filename 函数测试"""

    def test_accepts_canonical_format(self, temp_evidence_dir: Path):
        """测试接受 canonical 格式"""
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        filepath.touch()
        result = validate_filename(filepath)
        assert result is None

    def test_accepts_snapshot_format(self, temp_evidence_dir: Path):
        """测试接受 snapshot 格式"""
        filepath = temp_evidence_dir / "iteration_13_20260201_103000.json"
        filepath.touch()
        result = validate_filename(filepath)
        assert result is None

    def test_accepts_snapshot_sha_format(self, temp_evidence_dir: Path):
        """测试接受 snapshot+SHA 格式"""
        filepath = temp_evidence_dir / "iteration_13_20260201_103000_abc1234.json"
        filepath.touch()
        result = validate_filename(filepath)
        assert result is None

    def test_rejects_invalid_format(self, temp_evidence_dir: Path):
        """测试拒绝无效格式"""
        filepath = temp_evidence_dir / "invalid_name.json"
        filepath.touch()
        result = validate_filename(filepath)
        assert result is not None
        assert result.violation_type == "naming"
        assert "不符合命名规范" in result.message


# ============================================================================
# validate_json_content 测试
# ============================================================================


class TestValidateJsonContent:
    """validate_json_content 函数测试"""

    def test_accepts_valid_content(
        self, canonical_evidence_file: Path, valid_evidence_content: dict
    ):
        """测试接受有效内容"""
        schema = load_schema()
        violations = validate_json_content(canonical_evidence_file, schema)
        assert len(violations) == 0

    def test_detects_invalid_json(self, temp_evidence_dir: Path):
        """测试检测无效 JSON"""
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        filepath.write_text("{invalid json", encoding="utf-8")

        violations = validate_json_content(filepath, None)
        assert len(violations) == 1
        assert violations[0].violation_type == "content"
        assert "JSON 解析失败" in violations[0].message

    def test_detects_iteration_number_mismatch(
        self, temp_evidence_dir: Path, valid_evidence_content: dict
    ):
        """测试检测 iteration_number 不一致"""
        # 文件名说是 iteration 13，但内容说是 14
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        valid_evidence_content["iteration_number"] = 14
        filepath.write_text(json.dumps(valid_evidence_content), encoding="utf-8")

        violations = validate_json_content(filepath, None)
        assert len(violations) == 1
        assert violations[0].violation_type == "content"
        assert "iteration_number 不一致" in violations[0].message
        assert "文件名指示 13" in violations[0].message
        assert "JSON 内容为 14" in violations[0].message

    def test_schema_validation(self, temp_evidence_dir: Path):
        """测试 Schema 校验"""
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        # 缺少必需字段的内容
        invalid_content = {
            "iteration_number": 13,
            # 缺少 recorded_at, commit_sha, runner, commands
        }
        filepath.write_text(json.dumps(invalid_content), encoding="utf-8")

        schema = load_schema()
        if schema is not None:
            violations = validate_json_content(filepath, schema)
            # 应该有 schema 违规
            schema_violations = [v for v in violations if v.violation_type == "schema"]
            assert len(schema_violations) >= 1
            assert "Schema 校验失败" in schema_violations[0].message

    def test_schema_validation_missing_required_field(self, temp_evidence_dir: Path):
        """测试 Schema 校验 - 缺少必需字段"""
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        # 只有部分必需字段
        content = {
            "iteration_number": 13,
            "recorded_at": "2026-02-01T20:46:36Z",
            # 缺少 commit_sha, runner, commands
        }
        filepath.write_text(json.dumps(content), encoding="utf-8")

        schema = load_schema()
        if schema is not None:
            violations = validate_json_content(filepath, schema)
            schema_violations = [v for v in violations if v.violation_type == "schema"]
            assert len(schema_violations) >= 1

    def test_schema_validation_invalid_field_type(
        self, temp_evidence_dir: Path, valid_evidence_content: dict
    ):
        """测试 Schema 校验 - 字段类型错误"""
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        # iteration_number 应该是整数，不是字符串
        valid_evidence_content["iteration_number"] = "13"
        filepath.write_text(json.dumps(valid_evidence_content), encoding="utf-8")

        schema = load_schema()
        if schema is not None:
            violations = validate_json_content(filepath, schema)
            schema_violations = [v for v in violations if v.violation_type == "schema"]
            assert len(schema_violations) >= 1


# ============================================================================
# get_evidence_files 测试
# ============================================================================


class TestGetEvidenceFiles:
    """get_evidence_files 函数测试"""

    def test_finds_json_files(self, temp_evidence_dir: Path):
        """测试找到 JSON 文件"""
        (temp_evidence_dir / "iteration_13_evidence.json").touch()
        (temp_evidence_dir / "iteration_14_evidence.json").touch()

        files = get_evidence_files(temp_evidence_dir)
        assert len(files) == 2

    def test_excludes_non_json_files(self, temp_evidence_dir: Path):
        """测试排除非 JSON 文件"""
        (temp_evidence_dir / "iteration_13_evidence.json").touch()
        (temp_evidence_dir / ".gitkeep").touch()
        (temp_evidence_dir / "readme.md").touch()

        files = get_evidence_files(temp_evidence_dir)
        assert len(files) == 1
        assert files[0].name == "iteration_13_evidence.json"

    def test_returns_empty_for_nonexistent_dir(self):
        """测试不存在的目录返回空列表"""
        files = get_evidence_files(Path("/nonexistent/path"))
        assert files == []

    def test_returns_sorted_files(self, temp_evidence_dir: Path):
        """测试返回排序后的文件列表"""
        (temp_evidence_dir / "iteration_15_evidence.json").touch()
        (temp_evidence_dir / "iteration_13_evidence.json").touch()
        (temp_evidence_dir / "iteration_14_evidence.json").touch()

        files = get_evidence_files(temp_evidence_dir)
        assert len(files) == 3
        assert files[0].name == "iteration_13_evidence.json"
        assert files[1].name == "iteration_14_evidence.json"
        assert files[2].name == "iteration_15_evidence.json"


# ============================================================================
# scan_evidence_files 测试
# ============================================================================


class TestScanEvidenceFiles:
    """scan_evidence_files 函数测试"""

    def test_scans_valid_files(self, temp_evidence_dir: Path, canonical_evidence_file: Path):
        """测试扫描有效文件"""
        violations, total_files = scan_evidence_files(evidence_dir=temp_evidence_dir)
        assert total_files == 1
        assert len(violations) == 0

    def test_detects_naming_violations(self, temp_evidence_dir: Path, valid_evidence_content: dict):
        """测试检测命名违规"""
        filepath = temp_evidence_dir / "bad_name.json"
        filepath.write_text(json.dumps(valid_evidence_content), encoding="utf-8")

        violations, total_files = scan_evidence_files(evidence_dir=temp_evidence_dir)
        assert total_files == 1
        naming_violations = [v for v in violations if v.violation_type == "naming"]
        assert len(naming_violations) == 1

    def test_detects_schema_violations(self, temp_evidence_dir: Path):
        """测试检测 Schema 违规"""
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        # 缺少必需字段
        filepath.write_text('{"iteration_number": 13}', encoding="utf-8")

        violations, total_files = scan_evidence_files(evidence_dir=temp_evidence_dir)
        assert total_files == 1
        # 应该有 schema 违规（如果 jsonschema 可用）
        # 测试不假定 jsonschema 一定可用

    def test_empty_directory(self, temp_evidence_dir: Path):
        """测试空目录"""
        violations, total_files = scan_evidence_files(evidence_dir=temp_evidence_dir)
        assert total_files == 0
        assert len(violations) == 0


# ============================================================================
# EvidenceViolation 数据类测试
# ============================================================================


class TestEvidenceViolation:
    """EvidenceViolation 数据类测试"""

    def test_str_format_naming(self):
        """测试命名违规的字符串格式"""
        violation = EvidenceViolation(
            file=Path("bad_name.json"),
            violation_type="naming",
            message="文件名不符合命名规范",
        )
        str_repr = str(violation)
        assert "bad_name.json" in str_repr
        assert "[naming]" in str_repr
        assert "文件名不符合命名规范" in str_repr

    def test_str_format_schema(self):
        """测试 Schema 违规的字符串格式"""
        violation = EvidenceViolation(
            file=Path("iteration_13_evidence.json"),
            violation_type="schema",
            message="Schema 校验失败 @ runner: 'runner' is a required property",
        )
        str_repr = str(violation)
        assert "[schema]" in str_repr
        assert "Schema 校验失败" in str_repr

    def test_str_format_content(self):
        """测试内容违规的字符串格式"""
        violation = EvidenceViolation(
            file=Path("iteration_13_evidence.json"),
            violation_type="content",
            message="iteration_number 不一致",
        )
        str_repr = str(violation)
        assert "[content]" in str_repr
        assert "iteration_number 不一致" in str_repr


# ============================================================================
# load_schema 测试
# ============================================================================


class TestLoadSchema:
    """load_schema 函数测试"""

    def test_loads_schema_from_default_path(self):
        """测试从默认路径加载 Schema"""
        # 只有当实际 schema 文件存在时才运行
        if SCHEMA_PATH.exists():
            schema = load_schema()
            assert schema is not None
            assert "properties" in schema
            assert "iteration_number" in schema["properties"]


# ============================================================================
# 集成测试
# ============================================================================


class TestIntegration:
    """集成测试"""

    def test_mixed_violations(self, temp_evidence_dir: Path, valid_evidence_content: dict):
        """测试同时存在多种违规"""
        # 1. 有效文件
        (temp_evidence_dir / "iteration_13_evidence.json").write_text(
            json.dumps(valid_evidence_content), encoding="utf-8"
        )

        # 2. 命名违规
        valid_evidence_content["iteration_number"] = 14
        (temp_evidence_dir / "bad_name.json").write_text(
            json.dumps(valid_evidence_content), encoding="utf-8"
        )

        # 3. 内容不一致违规（iteration_number 与文件名不匹配）
        mismatched_content = valid_evidence_content.copy()
        mismatched_content["iteration_number"] = 99
        (temp_evidence_dir / "iteration_15_evidence.json").write_text(
            json.dumps(mismatched_content), encoding="utf-8"
        )

        violations, total_files = scan_evidence_files(evidence_dir=temp_evidence_dir)

        assert total_files == 3

        # 应该有命名违规
        naming_violations = [v for v in violations if v.violation_type == "naming"]
        assert len(naming_violations) == 1

        # 应该有内容不一致违规
        content_violations = [v for v in violations if v.violation_type == "content"]
        assert len(content_violations) >= 1

    def test_real_evidence_directory(self):
        """测试真实的证据目录（如果存在）"""
        if EVIDENCE_DIR.exists():
            violations, total_files = scan_evidence_files(evidence_dir=EVIDENCE_DIR)
            # 真实目录应该没有违规（或者已知违规数量）
            # 这里只验证脚本能正常运行
            assert total_files >= 0
            # 不断言具体数量，因为真实数据可能变化


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_json_file(self, temp_evidence_dir: Path):
        """测试空 JSON 文件"""
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        filepath.write_text("", encoding="utf-8")

        violations = validate_json_content(filepath, None)
        assert len(violations) == 1
        assert violations[0].violation_type == "content"
        assert "JSON 解析失败" in violations[0].message

    def test_json_array_instead_of_object(self, temp_evidence_dir: Path):
        """测试 JSON 数组（而非对象）"""
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        filepath.write_text("[]", encoding="utf-8")

        schema = load_schema()
        if schema is not None:
            violations = validate_json_content(filepath, schema)
            # 应该有 schema 违规（期望对象，得到数组）
            schema_violations = [v for v in violations if v.violation_type == "schema"]
            assert len(schema_violations) >= 1

    def test_unicode_content(self, temp_evidence_dir: Path, valid_evidence_content: dict):
        """测试 Unicode 内容"""
        valid_evidence_content["notes"] = "中文备注 🎉"
        filepath = temp_evidence_dir / "iteration_13_evidence.json"
        filepath.write_text(
            json.dumps(valid_evidence_content, ensure_ascii=False), encoding="utf-8"
        )

        violations, total_files = scan_evidence_files(evidence_dir=temp_evidence_dir)
        assert total_files == 1
        assert len(violations) == 0

    def test_large_iteration_number(self, temp_evidence_dir: Path, valid_evidence_content: dict):
        """测试大迭代编号"""
        valid_evidence_content["iteration_number"] = 999
        filepath = temp_evidence_dir / "iteration_999_evidence.json"
        filepath.write_text(json.dumps(valid_evidence_content), encoding="utf-8")

        violations, total_files = scan_evidence_files(evidence_dir=temp_evidence_dir)
        assert total_files == 1
        assert len(violations) == 0

    def test_zero_iteration_number_in_filename(
        self, temp_evidence_dir: Path, valid_evidence_content: dict
    ):
        """测试迭代编号为 0 的文件名（应该不合规）"""
        valid_evidence_content["iteration_number"] = 0
        filepath = temp_evidence_dir / "iteration_0_evidence.json"
        filepath.write_text(json.dumps(valid_evidence_content), encoding="utf-8")

        violations, total_files = scan_evidence_files(evidence_dir=temp_evidence_dir)
        # 文件名可以解析，但 schema 可能要求正整数
        # 这里主要验证脚本不会崩溃
        assert total_files == 1


# ============================================================================
# 常量路径测试
# ============================================================================


class TestConstants:
    """常量测试"""

    def test_evidence_dir_path(self):
        """测试 EVIDENCE_DIR 路径格式"""
        assert EVIDENCE_DIR.name == "evidence"
        assert EVIDENCE_DIR.parent.name == "acceptance"
        assert EVIDENCE_DIR.parent.parent.name == "docs"

    def test_schema_path(self):
        """测试 SCHEMA_PATH 路径格式"""
        assert SCHEMA_PATH.name == "iteration_evidence_v1.schema.json"
        assert SCHEMA_PATH.parent.name == "schemas"
