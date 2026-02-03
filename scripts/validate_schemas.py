#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_schemas.py - 统一 Schema 校验入口

功能:
  1. 校验 schemas/ 目录下所有 JSON Schema 文件的格式有效性
  2. 校验 schema 文件自身符合 JSON Schema Draft-2020-12 规范
  3. 校验每个 schema 中的 examples 是否符合自身 schema
  4. 校验 schemas/fixtures/ 目录下的样例数据是否符合对应 schema
  5. 输出详细的校验结果，支持 JSON 格式输出

使用方法:
  python scripts/validate_schemas.py                    # 校验所有 schema
  python scripts/validate_schemas.py --json            # JSON 格式输出
  python scripts/validate_schemas.py --schema <file>   # 校验指定 schema
  python scripts/validate_schemas.py --verbose         # 详细输出
  python scripts/validate_schemas.py --validate-fixtures  # 同时校验 fixtures 目录

退出码:
  0 - 所有校验通过
  1 - 存在校验失败
  2 - 参数错误 / 文件不存在

环境变量:
  SCHEMAS_DIR   自定义 schemas 目录路径（默认 schemas/）
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# 尝试导入 jsonschema
try:
    import jsonschema
    from jsonschema import Draft202012Validator, SchemaError, ValidationError
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False
    Draft202012Validator = None
    ValidationError = Exception
    SchemaError = Exception

# ============================================================================
# 常量定义
# ============================================================================

DEFAULT_SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"
DEFAULT_FIXTURES_DIR = "fixtures"  # 相对于 schemas/ 目录

# Schema 文件名与 fixtures 目录名的映射（去掉 .schema.json 后缀）
# 例: scm_sync_job_payload_v1.schema.json -> fixtures/scm_sync_job_payload_v1/
SCHEMA_FIXTURE_MAPPING = {
    "audit_event_v1.schema.json": "audit_event_v1",
    "iteration_evidence_v2.schema.json": "iteration_evidence_v2",
    "object_store_audit_event_v1.schema.json": "object_store_audit_event_v1",
    "scm_sync_job_payload_v1.schema.json": "scm_sync_job_payload_v1",
    "scm_sync_run_v1.schema.json": "scm_sync_run_v1",
}

# 需要校验的 schema 文件列表（相对于 schemas/ 目录）
SCHEMA_FILES = [
    "audit_event_v1.schema.json",
    "iteration_evidence_v2.schema.json",
    "iteration_gate_profiles_v1.schema.json",
    "iteration_toolchain_drift_map_v1.schema.json",
    "object_store_audit_event_v1.schema.json",
    "reliability_report_v1.schema.json",
    "workflow_contract_drift_report_v1.schema.json",
    "scm_sync_job_payload_v1.schema.json",
    "scm_sync_result_v1.schema.json",
    "scm_sync_run_v1.schema.json",
    "seek_query_packet_v1.schema.json",
    "unified_stack_verify_results_v1.schema.json",
    "openmemory_conflict.schema.json",
    "openmemory_patches.schema.json",
    "openmemory_upstream_lock.schema.json",
]

# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class FixtureValidationResult:
    """单个 fixture 文件的校验结果"""
    fixture_file: str
    schema_file: str
    is_valid_json: bool = True
    is_valid_against_schema: bool = True
    json_error: Optional[str] = None
    schema_error: Optional[str] = None

    @property
    def overall_valid(self) -> bool:
        return self.is_valid_json and self.is_valid_against_schema

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_file": self.fixture_file,
            "schema_file": self.schema_file,
            "is_valid_json": self.is_valid_json,
            "is_valid_against_schema": self.is_valid_against_schema,
            "overall_valid": self.overall_valid,
            "json_error": self.json_error,
            "schema_error": self.schema_error,
        }


@dataclass
class SchemaValidationResult:
    """单个 schema 文件的校验结果"""
    file_name: str
    file_path: str
    is_valid_json: bool = True
    is_valid_schema: bool = True
    examples_valid: bool = True
    json_error: Optional[str] = None
    schema_error: Optional[str] = None
    example_errors: list[dict] = field(default_factory=list)
    # 关联的 fixtures 校验结果
    fixture_results: list[FixtureValidationResult] = field(default_factory=list)

    @property
    def fixtures_valid(self) -> bool:
        if not self.fixture_results:
            return True
        return all(f.overall_valid for f in self.fixture_results)

    @property
    def overall_valid(self) -> bool:
        return self.is_valid_json and self.is_valid_schema and self.examples_valid and self.fixtures_valid

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "file_path": self.file_path,
            "is_valid_json": self.is_valid_json,
            "is_valid_schema": self.is_valid_schema,
            "examples_valid": self.examples_valid,
            "fixtures_valid": self.fixtures_valid,
            "overall_valid": self.overall_valid,
            "json_error": self.json_error,
            "schema_error": self.schema_error,
            "example_errors": self.example_errors,
            "fixture_results": [f.to_dict() for f in self.fixture_results],
        }


@dataclass
class ValidationReport:
    """整体校验报告"""
    schemas_dir: str
    total_schemas: int = 0
    passed_schemas: int = 0
    failed_schemas: int = 0
    # Fixtures 统计
    total_fixtures: int = 0
    passed_fixtures: int = 0
    failed_fixtures: int = 0
    fixtures_validated: bool = False
    results: list[SchemaValidationResult] = field(default_factory=list)
    has_jsonschema: bool = HAS_JSONSCHEMA

    @property
    def overall_valid(self) -> bool:
        schemas_ok = self.failed_schemas == 0 and self.has_jsonschema
        fixtures_ok = not self.fixtures_validated or self.failed_fixtures == 0
        return schemas_ok and fixtures_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemas_dir": self.schemas_dir,
            "total_schemas": self.total_schemas,
            "passed_schemas": self.passed_schemas,
            "failed_schemas": self.failed_schemas,
            "total_fixtures": self.total_fixtures,
            "passed_fixtures": self.passed_fixtures,
            "failed_fixtures": self.failed_fixtures,
            "fixtures_validated": self.fixtures_validated,
            "overall_valid": self.overall_valid,
            "has_jsonschema": self.has_jsonschema,
            "results": [r.to_dict() for r in self.results],
        }


# ============================================================================
# 颜色输出
# ============================================================================

class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'  # No Color

    @classmethod
    def disable(cls):
        """禁用颜色输出（用于非 TTY 环境）"""
        cls.RED = ''
        cls.GREEN = ''
        cls.YELLOW = ''
        cls.BLUE = ''
        cls.NC = ''

# 非 TTY 环境禁用颜色
if not sys.stdout.isatty():
    Colors.disable()


def log_info(msg: str):
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")


def log_success(msg: str):
    print(f"{Colors.GREEN}[PASS]{Colors.NC} {msg}")


def log_warn(msg: str):
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}")


def log_error(msg: str):
    print(f"{Colors.RED}[FAIL]{Colors.NC} {msg}")


# ============================================================================
# 校验逻辑
# ============================================================================

def validate_json_syntax(file_path: Path) -> tuple[bool, Optional[dict], Optional[str]]:
    """
    校验 JSON 文件语法

    Returns:
        (is_valid, parsed_data, error_message)
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return True, data, None
    except json.JSONDecodeError as e:
        return False, None, f"JSON 语法错误: {e}"
    except FileNotFoundError:
        return False, None, f"文件不存在: {file_path}"
    except Exception as e:
        return False, None, f"读取文件错误: {e}"


def validate_schema_definition(schema: dict) -> tuple[bool, Optional[str]]:
    """
    校验 schema 定义是否符合 JSON Schema Draft-2020-12 规范

    Returns:
        (is_valid, error_message)
    """
    if not HAS_JSONSCHEMA:
        return True, None  # 无法校验，跳过

    try:
        Draft202012Validator.check_schema(schema)
        return True, None
    except SchemaError as e:
        return False, f"Schema 定义错误: {e.message}"
    except Exception as e:
        return False, f"Schema 校验错误: {e}"


def validate_schema_examples(schema: dict) -> tuple[bool, list[dict]]:
    """
    校验 schema 中的 examples 是否符合 schema 定义

    Returns:
        (all_valid, list of errors)
    """
    if not HAS_JSONSCHEMA:
        return True, []  # 无法校验，跳过

    examples = schema.get("examples", [])
    if not examples:
        return True, []

    errors = []
    validator = Draft202012Validator(schema)

    for i, example in enumerate(examples):
        try:
            validator.validate(example)
        except ValidationError as e:
            errors.append({
                "example_index": i,
                "error_path": ".".join(str(p) for p in e.absolute_path),
                "error_message": e.message,
            })

    return len(errors) == 0, errors


def validate_fixture_against_schema(
    fixture_path: Path,
    schema: dict,
    schema_file_name: str,
) -> FixtureValidationResult:
    """
    校验单个 fixture 文件是否符合 schema

    Args:
        fixture_path: fixture 文件路径
        schema: 已加载的 schema dict
        schema_file_name: schema 文件名（用于日志）

    Returns:
        FixtureValidationResult
    """
    result = FixtureValidationResult(
        fixture_file=str(fixture_path),
        schema_file=schema_file_name,
    )

    # 1. 加载 fixture JSON
    is_valid_json, fixture_data, json_error = validate_json_syntax(fixture_path)
    result.is_valid_json = is_valid_json
    result.json_error = json_error

    if not is_valid_json:
        result.is_valid_against_schema = False
        return result

    # 2. 使用 schema 校验 fixture
    if not HAS_JSONSCHEMA:
        return result  # 无法校验，跳过

    try:
        validator = Draft202012Validator(schema)
        validator.validate(fixture_data)
        result.is_valid_against_schema = True
    except ValidationError as e:
        result.is_valid_against_schema = False
        error_path = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "(root)"
        result.schema_error = f"Path '{error_path}': {e.message}"
    except Exception as e:
        result.is_valid_against_schema = False
        result.schema_error = str(e)

    return result


def discover_fixtures_for_schema(
    schemas_dir: Path,
    schema_file_name: str,
) -> list[Path]:
    """
    发现指定 schema 对应的 fixture 文件

    Args:
        schemas_dir: schemas 目录路径
        schema_file_name: schema 文件名

    Returns:
        fixture 文件路径列表
    """
    fixtures_dir_name = SCHEMA_FIXTURE_MAPPING.get(schema_file_name)
    if not fixtures_dir_name:
        return []

    fixtures_dir = schemas_dir / DEFAULT_FIXTURES_DIR / fixtures_dir_name
    if not fixtures_dir.exists():
        return []

    return sorted(fixtures_dir.glob("*.json"))


def validate_single_schema(
    file_path: Path,
    validate_fixtures: bool = False,
    schemas_dir: Optional[Path] = None,
) -> SchemaValidationResult:
    """
    校验单个 schema 文件

    Args:
        file_path: schema 文件路径
        validate_fixtures: 是否校验关联的 fixtures
        schemas_dir: schemas 目录路径（用于发现 fixtures）

    Returns:
        SchemaValidationResult
    """
    result = SchemaValidationResult(
        file_name=file_path.name,
        file_path=str(file_path),
    )

    # 1. 校验 JSON 语法
    is_valid_json, schema, json_error = validate_json_syntax(file_path)
    result.is_valid_json = is_valid_json
    result.json_error = json_error

    if not is_valid_json:
        result.is_valid_schema = False
        result.examples_valid = False
        return result

    # 2. 校验 schema 定义
    is_valid_schema, schema_error = validate_schema_definition(schema)
    result.is_valid_schema = is_valid_schema
    result.schema_error = schema_error

    if not is_valid_schema:
        result.examples_valid = False
        return result

    # 3. 校验 examples
    examples_valid, example_errors = validate_schema_examples(schema)
    result.examples_valid = examples_valid
    result.example_errors = example_errors

    # 4. 校验 fixtures（可选）
    if validate_fixtures and schemas_dir:
        fixture_files = discover_fixtures_for_schema(schemas_dir, file_path.name)
        for fixture_file in fixture_files:
            fixture_result = validate_fixture_against_schema(
                fixture_path=fixture_file,
                schema=schema,
                schema_file_name=file_path.name,
            )
            result.fixture_results.append(fixture_result)

    return result


def validate_all_schemas(
    schemas_dir: Path,
    schema_files: Optional[list[str]] = None,
    verbose: bool = False,
    validate_fixtures: bool = False,
) -> ValidationReport:
    """
    校验所有 schema 文件

    Args:
        schemas_dir: schemas 目录路径
        schema_files: 要校验的文件列表（为 None 则使用默认列表）
        verbose: 是否输出详细信息
        validate_fixtures: 是否同时校验 fixtures 目录

    Returns:
        ValidationReport
    """
    report = ValidationReport(schemas_dir=str(schemas_dir))
    report.fixtures_validated = validate_fixtures

    if schema_files is None:
        schema_files = SCHEMA_FILES

    # 过滤存在的文件
    existing_files = []
    for file_name in schema_files:
        file_path = schemas_dir / file_name
        if file_path.exists():
            existing_files.append(file_name)
        elif verbose:
            log_warn(f"跳过不存在的文件: {file_name}")

    report.total_schemas = len(existing_files)

    for file_name in existing_files:
        file_path = schemas_dir / file_name

        if verbose:
            log_info(f"校验 {file_name}...")

        result = validate_single_schema(
            file_path,
            validate_fixtures=validate_fixtures,
            schemas_dir=schemas_dir,
        )
        report.results.append(result)

        # 统计 fixtures
        if validate_fixtures and result.fixture_results:
            for fixture_result in result.fixture_results:
                report.total_fixtures += 1
                if fixture_result.overall_valid:
                    report.passed_fixtures += 1
                else:
                    report.failed_fixtures += 1

        # 判断 schema 是否通过（需要考虑 fixtures 校验结果）
        if result.overall_valid:
            report.passed_schemas += 1
            if verbose:
                log_success(f"{file_name} 校验通过")
                # 输出 fixtures 校验详情
                if result.fixture_results:
                    for fr in result.fixture_results:
                        fixture_name = Path(fr.fixture_file).name
                        if fr.overall_valid:
                            print(f"    [✓] fixture: {fixture_name}")
                        else:
                            print(f"    [✗] fixture: {fixture_name}")
        else:
            report.failed_schemas += 1
            if verbose:
                log_error(f"{file_name} 校验失败")
                if result.json_error:
                    print(f"    JSON 错误: {result.json_error}")
                if result.schema_error:
                    print(f"    Schema 错误: {result.schema_error}")
                for err in result.example_errors:
                    print(f"    Example[{err['example_index']}] 错误: {err['error_message']}")
                # 输出失败的 fixtures
                for fr in result.fixture_results:
                    if not fr.overall_valid:
                        fixture_name = Path(fr.fixture_file).name
                        print(f"    Fixture '{fixture_name}' 失败:")
                        if fr.json_error:
                            print(f"      JSON 错误: {fr.json_error}")
                        if fr.schema_error:
                            print(f"      Schema 错误: {fr.schema_error}")

    return report


# ============================================================================
# 主入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="统一 Schema 校验入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="JSON 格式输出结果",
    )

    parser.add_argument(
        "--schema",
        type=str,
        default=None,
        help="仅校验指定的 schema 文件（相对于 schemas/ 目录）",
    )

    parser.add_argument(
        "--schemas-dir",
        type=Path,
        default=None,
        help="schemas 目录路径（默认 schemas/）",
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出",
    )

    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="输出结果到文件",
    )

    parser.add_argument(
        "--validate-fixtures",
        action="store_true",
        dest="validate_fixtures",
        help="同时校验 schemas/fixtures/ 目录下的样例数据",
    )

    args = parser.parse_args()

    # 确定 schemas 目录
    schemas_dir = args.schemas_dir
    if schemas_dir is None:
        schemas_dir = Path(os.environ.get("SCHEMAS_DIR", DEFAULT_SCHEMAS_DIR))

    if not schemas_dir.exists():
        log_error(f"schemas 目录不存在: {schemas_dir}")
        sys.exit(2)

    # 检查 jsonschema 依赖
    if not HAS_JSONSCHEMA:
        log_warn("jsonschema 未安装，仅执行 JSON 语法校验")
        if not args.json_output:
            log_info("安装 jsonschema: pip install jsonschema")

    # 确定要校验的文件
    schema_files = None
    if args.schema:
        schema_files = [args.schema]

    # 执行校验
    if not args.json_output and not args.verbose:
        log_info(f"开始校验 schemas 目录: {schemas_dir}")
        if args.validate_fixtures:
            log_info("启用 fixtures 校验")

    report = validate_all_schemas(
        schemas_dir=schemas_dir,
        schema_files=schema_files,
        verbose=args.verbose and not args.json_output,
        validate_fixtures=args.validate_fixtures,
    )

    # 输出结果
    if args.json_output:
        output_data = report.to_dict()
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2)
            log_info(f"结果已写入: {args.output}")
        else:
            print(json.dumps(output_data, ensure_ascii=False, indent=2))
    else:
        print("")
        print("=" * 50)
        print("Schema 校验结果汇总")
        print("=" * 50)
        print(f"  目录:     {report.schemas_dir}")
        print(f"  总计:     {report.total_schemas}")
        print(f"  通过:     {report.passed_schemas}")
        print(f"  失败:     {report.failed_schemas}")

        # Fixtures 统计
        if report.fixtures_validated:
            print("")
            print("Fixtures 校验结果:")
            print(f"  总计:     {report.total_fixtures}")
            print(f"  通过:     {report.passed_fixtures}")
            print(f"  失败:     {report.failed_fixtures}")
        print("")

        # 输出失败详情
        if report.failed_schemas > 0:
            print("失败的 schema:")
            for result in report.results:
                if not result.overall_valid:
                    print(f"  - {result.file_name}")
                    if result.json_error:
                        print(f"      JSON 错误: {result.json_error}")
                    if result.schema_error:
                        print(f"      Schema 错误: {result.schema_error}")
                    for err in result.example_errors:
                        print(f"      Example[{err['example_index']}]: {err['error_message']}")
                    # 输出失败的 fixtures
                    for fr in result.fixture_results:
                        if not fr.overall_valid:
                            fixture_name = Path(fr.fixture_file).name
                            print(f"      Fixture '{fixture_name}':")
                            if fr.json_error:
                                print(f"        JSON 错误: {fr.json_error}")
                            if fr.schema_error:
                                print(f"        Schema 错误: {fr.schema_error}")
            print("")

        # 输出通过列表
        if args.verbose and report.passed_schemas > 0:
            print("通过的 schema:")
            for result in report.results:
                if result.overall_valid:
                    print(f"  [✓] {result.file_name}")
                    # 同时显示通过的 fixtures
                    for fr in result.fixture_results:
                        if fr.overall_valid:
                            fixture_name = Path(fr.fixture_file).name
                            print(f"      [✓] {fixture_name}")
            print("")

        if report.overall_valid:
            log_success("所有 schema 校验通过！")
        else:
            log_error("存在 schema 校验失败！")

        # 写入文件
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
            log_info(f"结果已写入: {args.output}")

    sys.exit(0 if report.overall_valid else 1)


if __name__ == "__main__":
    main()
