#!/usr/bin/env python3
"""
Workflow Contract Validator

校验 GitHub Actions workflow 文件是否符合 workflow_contract.v2.json 定义的合约。

功能:
- 读取 workflow_contract.v2.json
- 解析 .github/workflows/*.yml
- 输出清晰的失败信息（指出具体文件/键名/引用点）
- 对 step name 变化做 diff 提示（旧值/新值）

用法:
    python scripts/ci/validate_workflows.py
    python scripts/ci/validate_workflows.py --contract scripts/ci/workflow_contract.v2.json
    python scripts/ci/validate_workflows.py --json  # 输出 JSON 格式
"""

import argparse
import ast
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# Optional jsonschema support
try:
    from jsonschema import Draft7Validator
    from jsonschema import ValidationError as JsonSchemaValidationError

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False
    JsonSchemaValidationError = Exception  # type: ignore

from scripts.ci.workflow_contract_common import (
    MakeTargetUsage,
    build_workflows_view,
    discover_workflow_keys,
    extract_make_targets_from_workflow,
    find_fuzzy_match,
    normalize_artifact_path,
    parse_makefile_targets,
)

# ============================================================================
# Error Types and Warning Types - 统一定义
# ============================================================================
#
# 所有 error_type 和 warning_type 的统一定义，便于维护和测试覆盖。
#
# 版本策略：
#   - 新增 error_type: Minor (0.X.0)
#   - 弃用 error_type: Major (X.0.0) - 需提供迁移路径
#   - 修改 error_type 含义: Major (X.0.0)
#
# 详见 docs/ci_nightly_workflow_refactor/contract.md 第 13 章
#


# ValidationError.error_type 集合
class ErrorTypes:
    """ValidationError 的 error_type 常量定义"""

    # 文件/解析错误
    CONTRACT_NOT_FOUND = "contract_not_found"
    CONTRACT_PARSE_ERROR = "contract_parse_error"
    SCHEMA_PARSE_ERROR = "schema_parse_error"
    SCHEMA_ERROR = "schema_error"
    WORKFLOW_NOT_FOUND = "workflow_not_found"
    WORKFLOW_PARSE_ERROR = "workflow_parse_error"
    MAKEFILE_NOT_FOUND = "makefile_not_found"

    # Job 相关错误
    MISSING_JOB = "missing_job"
    MISSING_JOB_ID = "missing_job_id"
    EXTRA_JOB_NOT_IN_CONTRACT = "extra_job_not_in_contract"
    FROZEN_JOB_NAME_CHANGED = "frozen_job_name_changed"

    # Step 相关错误
    MISSING_STEP = "missing_step"
    FROZEN_STEP_NAME_CHANGED = "frozen_step_name_changed"

    # Output/Env 相关错误
    MISSING_OUTPUT = "missing_output"
    MISSING_ENV_VAR = "missing_env_var"

    # Artifact 相关错误
    MISSING_ARTIFACT_PATH = "missing_artifact_path"

    # Makefile 相关错误
    MISSING_MAKEFILE_TARGET = "missing_makefile_target"
    UNDECLARED_MAKE_TARGET = "undeclared_make_target"

    # Label 相关错误
    LABEL_MISSING_IN_SCRIPT = "label_missing_in_script"
    LABEL_MISSING_IN_CONTRACT = "label_missing_in_contract"

    # Contract 内部一致性错误
    CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH = "contract_job_ids_names_length_mismatch"
    CONTRACT_JOB_IDS_DUPLICATE = "contract_job_ids_duplicate"
    CONTRACT_REQUIRED_JOB_ID_DUPLICATE = "contract_required_job_id_duplicate"
    CONTRACT_REQUIRED_JOB_NOT_IN_JOB_IDS = "contract_required_job_not_in_job_ids"

    # Contract Frozen 一致性错误（--require-frozen-consistency 模式）
    CONTRACT_FROZEN_STEP_MISSING = "contract_frozen_step_missing"
    CONTRACT_FROZEN_JOB_MISSING = "contract_frozen_job_missing"
    UNFROZEN_REQUIRED_STEP = "unfrozen_required_step"
    UNFROZEN_REQUIRED_JOB = "unfrozen_required_job"


# ValidationWarning.warning_type 集合
class WarningTypes:
    """ValidationWarning 的 warning_type 常量定义"""

    # Schema 相关警告
    SCHEMA_SKIP = "schema_skip"

    # Job 相关警告
    JOB_NAME_CHANGED = "job_name_changed"
    JOB_NAME_MISMATCH = "job_name_mismatch"
    EXTRA_JOB_NOT_IN_CONTRACT = "extra_job_not_in_contract"

    # Step 相关警告
    STEP_NAME_CHANGED = "step_name_changed"
    STEP_NAME_ALIAS_MATCHED = "step_name_alias_matched"  # Step found via alias mapping

    # Frozen 相关警告（非 strict 模式）
    UNFROZEN_REQUIRED_STEP = "unfrozen_required_step"
    UNFROZEN_REQUIRED_JOB = "unfrozen_required_job"

    # Label 相关警告
    LABEL_SCRIPT_PARSE_WARNING = "label_script_parse_warning"


# 严格模式下阻断 CI 的 error_type 集合
# 这些错误会导致 validate-workflows-strict 失败
CRITICAL_ERROR_TYPES = frozenset(
    {
        ErrorTypes.CONTRACT_NOT_FOUND,
        ErrorTypes.CONTRACT_PARSE_ERROR,
        ErrorTypes.SCHEMA_ERROR,
        ErrorTypes.WORKFLOW_NOT_FOUND,
        ErrorTypes.MISSING_JOB,
        ErrorTypes.MISSING_JOB_ID,
        ErrorTypes.MISSING_STEP,
        ErrorTypes.FROZEN_STEP_NAME_CHANGED,
        ErrorTypes.FROZEN_JOB_NAME_CHANGED,
        ErrorTypes.MISSING_OUTPUT,
        ErrorTypes.MISSING_ENV_VAR,
        ErrorTypes.MISSING_ARTIFACT_PATH,
        ErrorTypes.MISSING_MAKEFILE_TARGET,
        ErrorTypes.UNDECLARED_MAKE_TARGET,
        ErrorTypes.LABEL_MISSING_IN_SCRIPT,
        ErrorTypes.LABEL_MISSING_IN_CONTRACT,
        ErrorTypes.CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH,
        ErrorTypes.CONTRACT_JOB_IDS_DUPLICATE,
        ErrorTypes.CONTRACT_REQUIRED_JOB_ID_DUPLICATE,
        ErrorTypes.CONTRACT_REQUIRED_JOB_NOT_IN_JOB_IDS,
    }
)

# --strict 模式下 WARNING 提升为 ERROR 的 warning_type 集合
STRICT_PROMOTED_WARNING_TYPES = frozenset(
    {
        WarningTypes.JOB_NAME_CHANGED,
        WarningTypes.JOB_NAME_MISMATCH,
        WarningTypes.STEP_NAME_CHANGED,
        WarningTypes.EXTRA_JOB_NOT_IN_CONTRACT,
    }
)


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class ValidationError:
    """验证错误"""

    workflow: str
    file: str
    error_type: str  # missing_job, missing_step, missing_output, missing_env_var, step_name_changed, schema_error
    key: str
    message: str
    expected: Optional[str] = None
    actual: Optional[str] = None
    location: Optional[str] = None  # e.g., "jobs.detect-changes.steps[2]"


@dataclass
class ValidationWarning:
    """验证警告（如 step name 变化）"""

    workflow: str
    file: str
    warning_type: str
    key: str
    message: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    location: Optional[str] = None


@dataclass
class ValidationResult:
    """验证结果"""

    success: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationWarning] = field(default_factory=list)
    validated_workflows: list[str] = field(default_factory=list)
    skipped_workflows: list[str] = field(default_factory=list)


# ============================================================================
# Makefile and Workflow Parsing Utilities
# ============================================================================

# Ignore list for make targets that use variables or complex patterns
# These are excluded from workflow->contract validation
MAKE_TARGET_IGNORE_LIST = {
    # Patterns with variables
    "deploy",  # May be called with different parameters
    # make -C subdirectory targets (handled separately)
    # False positives from echo statements
    "targets",  # From: echo "... make targets ..." (not an actual make call)
}


def extract_workflow_make_calls(workflow_path: Path) -> list[MakeTargetUsage]:
    """
    Extract make target calls from a GitHub Actions workflow file.

    Args:
        workflow_path: Path to the workflow YAML file

    Returns:
        MakeTargetUsage 列表（target + workflow/job/step 上下文）
    """
    return extract_make_targets_from_workflow(workflow_path)


# ============================================================================
# Artifact Path Extraction Utilities
# ============================================================================


def extract_upload_artifact_paths(workflow_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    从 workflow 中提取所有 actions/upload-artifact 步骤的 path 配置。

    Args:
        workflow_data: 解析后的 workflow YAML 数据

    Returns:
        List of dicts with keys:
        - job_id: Job ID
        - step_name: Step name (if available)
        - step_index: Step index in job
        - paths: List of extracted paths (split from multiline)
        - raw_path: Original path value
    """
    if not workflow_data or "jobs" not in workflow_data:
        return []

    results = []

    for job_id, job_data in workflow_data.get("jobs", {}).items():
        if not isinstance(job_data, dict):
            continue

        for step_index, step in enumerate(job_data.get("steps", [])):
            if not isinstance(step, dict):
                continue

            uses = step.get("uses", "")
            # Match actions/upload-artifact@v* (any version)
            if not re.match(r"actions/upload-artifact@v\d+", uses):
                continue

            with_block = step.get("with", {})
            if not isinstance(with_block, dict):
                continue

            path_value = with_block.get("path", "")
            if not path_value:
                continue

            # Parse multiline path values (YAML literal or folded)
            # Split by newlines and filter empty lines
            if isinstance(path_value, str):
                paths = [p.strip() for p in path_value.strip().split("\n") if p.strip()]
            else:
                paths = [str(path_value)]

            results.append(
                {
                    "job_id": job_id,
                    "step_name": step.get("name", ""),
                    "step_index": step_index,
                    "paths": paths,
                    "raw_path": path_value,
                }
            )

    return results


def _is_glob_pattern(path: str) -> bool:
    """
    检查路径是否包含 glob 模式字符。

    Args:
        path: 要检查的路径

    Returns:
        如果路径包含 glob 模式字符 (*?[]) 返回 True
    """
    return any(c in path for c in "*?[]")


def _normalize_for_comparison(path: str) -> str:
    """
    标准化路径用于比较。

    使用 workflow_contract_common.normalize_artifact_path 进行标准化，
    但对于空路径返回空字符串而非抛出异常。

    Args:
        path: 要标准化的路径

    Returns:
        标准化后的路径
    """
    try:
        return normalize_artifact_path(path, allow_empty=True)
    except Exception:
        return path


def _path_matches(uploaded: str, required_path: str) -> bool:
    """
    检查上传路径是否匹配 required_path。

    匹配规则：
    1. 首先对两个路径进行标准化（统一分隔符、处理 ./、去除重复斜杠）
    2. 如果 required_path 含有 glob 字符 (*?[])，使用 fnmatch.fnmatch
    3. 如果 required_path 以 '/' 结尾，视为目录匹配：
       - uploaded 在该目录下（startswith）
       - 或者 uploaded + '/' == required_path（目录本身）
    4. 否则做精确匹配

    Args:
        uploaded: 实际上传的路径
        required_path: 必需的路径（可能是 glob 模式、目录或文件）

    Returns:
        如果匹配返回 True
    """
    # 标准化路径
    uploaded = _normalize_for_comparison(uploaded)
    required_path = _normalize_for_comparison(required_path)

    # 空路径不匹配
    if not uploaded or not required_path:
        return False

    # 规则 1: glob 模式匹配
    if _is_glob_pattern(required_path):
        return fnmatch.fnmatch(uploaded, required_path)

    # 规则 2: 目录匹配（以 '/' 结尾）
    if required_path.endswith("/"):
        # uploaded 在该目录下
        if uploaded.startswith(required_path):
            return True
        # uploaded + '/' 等于 required_path（目录本身）
        if uploaded.rstrip("/") + "/" == required_path:
            return True
        return False

    # 规则 3: 精确匹配
    return uploaded == required_path


def check_artifact_path_coverage(
    upload_steps: list[dict[str, Any]],
    required_paths: list[str],
    step_name_filter: Optional[list[str]] = None,
) -> tuple[list[str], list[str]]:
    """
    检查 required_paths 是否被 upload steps 覆盖。

    匹配规则：
    1. 首先对所有路径进行标准化（统一分隔符、处理 ./、去除重复斜杠）
    2. 如果 required_path 含有 glob 字符 (*?[])，使用 fnmatch.fnmatch
    3. 如果 required_path 以 '/' 结尾，视为目录匹配
    4. 否则做精确匹配

    Args:
        upload_steps: extract_upload_artifact_paths 的返回结果
        required_paths: 必需的 artifact 路径列表
        step_name_filter: 可选的 step name 过滤器（仅检查这些 step）

    Returns:
        Tuple of (covered_paths, missing_paths)
        注意：返回的是原始 required_paths 中的路径，而非标准化后的路径
    """
    # 收集所有上传的路径（标准化）
    all_uploaded_paths: set[str] = set()

    for step in upload_steps:
        # 如果有 step name 过滤器，只检查匹配的 step
        if step_name_filter:
            step_name = step.get("step_name", "")
            if not any(
                filter_name.lower() in step_name.lower() for filter_name in step_name_filter
            ):
                continue

        for path in step.get("paths", []):
            normalized = _normalize_for_comparison(path)
            if normalized:  # 跳过空路径
                all_uploaded_paths.add(normalized)

    covered = []
    missing = []

    for required_path in required_paths:
        found = False
        for uploaded in all_uploaded_paths:
            if _path_matches(uploaded, required_path):
                found = True
                break

        if found:
            covered.append(required_path)
        else:
            missing.append(required_path)

    return covered, missing


# ============================================================================
# Workflow Validator
# ============================================================================


class WorkflowContractValidator:
    """Workflow 合约验证器"""

    def __init__(
        self,
        contract_path: Path,
        workspace_root: Path,
        require_job_coverage: bool = False,
    ):
        self.contract_path = contract_path
        self.workspace_root = workspace_root
        self.contract: dict[str, Any] = {}
        self.frozen_steps: set[str] = set()  # 冻结的 step name 集合
        self.frozen_job_names: set[str] = set()  # 冻结的 job name 集合
        self.step_name_aliases: dict[str, list[str]] = {}  # canonical step -> aliases
        self.require_job_coverage = require_job_coverage  # extra jobs 检测策略
        self.result = ValidationResult(success=True)

    def load_contract(self) -> bool:
        """加载合约文件"""
        if not self.contract_path.exists():
            self.result.errors.append(
                ValidationError(
                    workflow="",
                    file=str(self.contract_path),
                    error_type="contract_not_found",
                    key="",
                    message=f"Contract file not found: {self.contract_path}",
                )
            )
            self.result.success = False
            return False

        try:
            with open(self.contract_path, "r", encoding="utf-8") as f:
                self.contract = json.load(f)

            # 加载 frozen_step_text.allowlist
            frozen_step_text = self.contract.get("frozen_step_text", {})
            self.frozen_steps = set(frozen_step_text.get("allowlist", []))

            # 加载 frozen_job_names.allowlist（如果存在）
            frozen_job_names_config = self.contract.get("frozen_job_names", {})
            self.frozen_job_names = set(frozen_job_names_config.get("allowlist", []))

            # 加载 step_name_aliases（如果存在）
            # 格式: { "canonical_step_name": ["alias1", "alias2", ...], ... }
            step_name_aliases_config = self.contract.get("step_name_aliases", {})
            self.step_name_aliases = {
                k: v for k, v in step_name_aliases_config.items() if not k.startswith("_")
            }

            return True
        except json.JSONDecodeError as e:
            self.result.errors.append(
                ValidationError(
                    workflow="",
                    file=str(self.contract_path),
                    error_type="contract_parse_error",
                    key="",
                    message=f"Failed to parse contract JSON: {e}",
                )
            )
            self.result.success = False
            return False

    def validate_schema(self) -> bool:
        """
        使用 JSON Schema 校验合约文件结构。

        如果存在 schema 文件且已安装 jsonschema 库，则执行校验。
        校验失败会添加详细错误信息（字段路径/期望/实际）。

        Returns:
            bool: 校验通过返回 True，失败返回 False
        """
        # 查找 schema 文件（与 contract 同目录）
        schema_path = self.contract_path.parent / "workflow_contract.v2.schema.json"

        if not schema_path.exists():
            # schema 文件不存在，跳过校验
            return True

        if not HAS_JSONSCHEMA:
            # jsonschema 库未安装，添加警告但不阻止
            self.result.warnings.append(
                ValidationWarning(
                    workflow="",
                    file=str(schema_path),
                    warning_type="schema_skip",
                    key="jsonschema",
                    message="jsonschema library not installed, skipping schema validation",
                )
            )
            return True

        # 加载 schema
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
        except json.JSONDecodeError as e:
            self.result.errors.append(
                ValidationError(
                    workflow="",
                    file=str(schema_path),
                    error_type="schema_parse_error",
                    key="",
                    message=f"Failed to parse schema JSON: {e}",
                )
            )
            self.result.success = False
            return False

        # 执行 schema 校验
        validator = Draft7Validator(schema)
        errors = list(validator.iter_errors(self.contract))

        if errors:
            for error in errors:
                # 构建字段路径
                path = (
                    ".".join(str(p) for p in error.absolute_path)
                    if error.absolute_path
                    else "(root)"
                )

                # 获取期望和实际值
                expected = self._format_schema_expected(error)
                actual = (
                    self._format_value(error.instance) if error.instance is not None else "null"
                )

                self.result.errors.append(
                    ValidationError(
                        workflow="",
                        file=str(self.contract_path),
                        error_type="schema_error",
                        key=path,
                        message=error.message,
                        expected=expected,
                        actual=actual,
                        location=f"$.{path}" if path != "(root)" else "$",
                    )
                )

            self.result.success = False
            return False

        return True

    def _format_schema_expected(self, error: "JsonSchemaValidationError") -> str:
        """格式化 schema 校验的期望值描述"""
        if error.validator == "type":
            return f"type: {error.validator_value}"
        elif error.validator == "required":
            return f"required fields: {error.validator_value}"
        elif error.validator == "pattern":
            # 提供人类可读的 pattern 说明
            pattern = error.validator_value
            pattern_hints = {
                "^[0-9]+\\.[0-9]+\\.[0-9]+$": "semver format (e.g., 2.5.0)",
                "^[0-9]{4}-[0-9]{2}-[0-9]{2}$": "date format YYYY-MM-DD",
                "^[a-z][a-z0-9_-]*$": "lowercase identifier (kebab-case or snake_case)",
                "^[A-Z][A-Z0-9_]*$": "UPPER_SNAKE_CASE",
                "^\\.github/workflows/[a-z][a-z0-9_-]*\\.yml$": ".github/workflows/<name>.yml",
                "^\\.github/workflows/ci\\.yml$": ".github/workflows/ci.yml",
                "^\\.github/workflows/nightly\\.yml$": ".github/workflows/nightly.yml",
                "^_[a-z][a-z0-9_]*$": "underscore-prefixed field (e.g., _comment)",
                "^_changelog_v[0-9]+\\.[0-9]+\\.[0-9]+$": "changelog field (e.g., _changelog_v2.5.0)",
                "^workflow_contract\\.v2\\.schema\\.json$": "workflow_contract.v2.schema.json",
                "^[a-z][a-z0-9:_-]*$": "lowercase with optional colon (e.g., openmemory:freeze-override)",
            }
            hint = pattern_hints.get(pattern, pattern)
            return f"pattern: {hint}"
        elif error.validator == "enum":
            return f"one of: {error.validator_value}"
        elif error.validator == "additionalProperties":
            # 提供更详细的 additionalProperties 错误说明
            if error.absolute_path:
                path_str = ".".join(str(p) for p in error.absolute_path)
                return (
                    f"no additional properties allowed at '{path_str}' (use ^_ prefix for comments)"
                )
            return "no additional properties allowed (use ^_ prefix for comments)"
        elif error.validator == "minLength":
            return f"minimum length: {error.validator_value} characters"
        elif error.validator == "maxLength":
            return f"maximum length: {error.validator_value} characters"
        elif error.validator == "minItems":
            return f"minimum items: {error.validator_value}"
        elif error.validator == "maxItems":
            return f"maximum items: {error.validator_value}"
        elif error.validator == "uniqueItems":
            return "items must be unique (no duplicates)"
        elif error.validator == "allOf":
            return "must satisfy all schema requirements"
        elif error.validator == "anyOf":
            return "must satisfy at least one schema requirement"
        elif error.validator == "oneOf":
            return "must satisfy exactly one schema requirement"
        elif error.validator == "not":
            return "must not match the specified schema"
        elif error.validator == "const":
            return f"must be exactly: {error.validator_value}"
        elif error.validator == "minimum":
            return f"minimum value: {error.validator_value}"
        elif error.validator == "maximum":
            return f"maximum value: {error.validator_value}"
        else:
            return str(error.validator_value) if error.validator_value else "(see schema)"

    def _format_value(self, value: Any) -> str:
        """格式化值用于显示"""
        if isinstance(value, str):
            if len(value) == 0:
                return '""(empty string)'
            return f'"{value}"' if len(value) <= 50 else f'"{value[:47]}..."'
        elif isinstance(value, dict):
            if len(value) == 0:
                return "{} (empty object)"
            keys_preview = ", ".join(list(value.keys())[:3])
            if len(value) > 3:
                keys_preview += ", ..."
            return f"object with {len(value)} keys: {{{keys_preview}}}"
        elif isinstance(value, list):
            if len(value) == 0:
                return "[] (empty array)"
            return f"array with {len(value)} items"
        elif isinstance(value, bool):
            return str(value).lower()
        elif isinstance(value, (int, float)):
            return str(value)
        elif value is None:
            return "null"
        else:
            return str(value)

    def load_workflow(self, workflow_file: Path) -> Optional[dict[str, Any]]:
        """加载 workflow 文件"""
        if not workflow_file.exists():
            return None

        try:
            with open(workflow_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except yaml.YAMLError as e:
            self.result.errors.append(
                ValidationError(
                    workflow="",
                    file=str(workflow_file),
                    error_type="workflow_parse_error",
                    key="",
                    message=f"Failed to parse workflow YAML: {e}",
                )
            )
            return None

    def validate_job(
        self,
        workflow_name: str,
        workflow_file: str,
        workflow_data: dict[str, Any],
        job_contract: dict[str, Any],
    ) -> bool:
        """验证单个 job"""
        job_id = job_contract["id"]
        job_name = job_contract.get("name", "")
        required_steps = job_contract.get("required_steps", [])
        required_outputs = job_contract.get("required_outputs", [])

        jobs = workflow_data.get("jobs", {})

        # 检查 job 是否存在
        if job_id not in jobs:
            self.result.errors.append(
                ValidationError(
                    workflow=workflow_name,
                    file=workflow_file,
                    error_type="missing_job",
                    key=job_id,
                    message=f"Required job '{job_id}' not found in workflow",
                    expected=job_name,
                    location=f"jobs.{job_id}",
                )
            )
            self.result.success = False
            return False

        job_data = jobs[job_id]
        actual_job_name = job_data.get("name", "")

        # 检查 job name 是否匹配（警告级别）
        if job_name and actual_job_name != job_name:
            self.result.warnings.append(
                ValidationWarning(
                    workflow=workflow_name,
                    file=workflow_file,
                    warning_type="job_name_changed",
                    key=job_id,
                    message=f"Job name changed for '{job_id}'",
                    old_value=job_name,
                    new_value=actual_job_name,
                    location=f"jobs.{job_id}.name",
                )
            )

        # 获取实际 step names
        actual_steps = job_data.get("steps", [])
        actual_step_names = [step.get("name", "") for step in actual_steps]

        # 检查 required steps
        for required_step in required_steps:
            if required_step not in actual_step_names:
                # 首先检查是否通过 alias 映射能找到匹配
                alias_match = self._find_alias_match(required_step, actual_step_names)
                if alias_match:
                    # 通过 alias 找到匹配：报告为 WARNING（step_name_alias_matched）
                    self.result.warnings.append(
                        ValidationWarning(
                            workflow=workflow_name,
                            file=workflow_file,
                            warning_type="step_name_alias_matched",
                            key=required_step,
                            message=(
                                f"Required step '{required_step}' not found by exact name in job '{job_id}', "
                                f"but matched via alias '{alias_match}'. Consider updating the workflow "
                                f"to use the canonical name, or add '{alias_match}' to step_name_aliases."
                            ),
                            old_value=required_step,
                            new_value=alias_match,
                            location=f"jobs.{job_id}.steps",
                        )
                    )
                    continue  # alias 匹配成功，跳过后续检查

                # 尝试模糊匹配（部分匹配）
                fuzzy_match = self._find_fuzzy_match(required_step, actual_step_names)

                # 检查是否属于冻结的 step name
                is_frozen = required_step in self.frozen_steps

                if fuzzy_match:
                    if is_frozen:
                        # 冻结的 step name 改名：报告为 ERROR
                        self.result.errors.append(
                            ValidationError(
                                workflow=workflow_name,
                                file=workflow_file,
                                error_type="frozen_step_name_changed",
                                key=required_step,
                                message=(
                                    f"Frozen step '{required_step}' was renamed to '{fuzzy_match}' in job '{job_id}'. "
                                    f"此 step 属于冻结文案，不能改名。如确需改名，必须满足以下最小组合:\n"
                                    f"  【必需步骤 - 缺一不可】\n"
                                    f"  1. 更新 scripts/ci/workflow_contract.v2.json:\n"
                                    f"     - frozen_step_text.allowlist: 添加新名称，移除旧名称\n"
                                    f"     - required_jobs[].required_steps: 如有引用，同步更新\n"
                                    f"  2. 版本 bump（使用 bump_workflow_contract_version.py）:\n"
                                    f"     - python scripts/ci/bump_workflow_contract_version.py minor --message \"Rename frozen step: {required_step} -> {fuzzy_match}\"\n"
                                    f"  3. 更新 docs/ci_nightly_workflow_refactor/contract.md:\n"
                                    f"     - 'Frozen Step Names' 节 (contract.md#52-frozen-step-names)\n"
                                    f"  4. 运行 make validate-workflows-strict 验证\n"
                                    f"  详见 maintenance.md#62-冻结-step-rename-标准流程"
                                ),
                                expected=required_step,
                                actual=fuzzy_match,
                                location=f"jobs.{job_id}.steps",
                            )
                        )
                        self.result.success = False
                    else:
                        # 非冻结的 step name 改名：仅 WARNING
                        self.result.warnings.append(
                            ValidationWarning(
                                workflow=workflow_name,
                                file=workflow_file,
                                warning_type="step_name_changed",
                                key=required_step,
                                message=f"Step name changed in job '{job_id}'",
                                old_value=required_step,
                                new_value=fuzzy_match,
                                location=f"jobs.{job_id}.steps",
                            )
                        )
                else:
                    self.result.errors.append(
                        ValidationError(
                            workflow=workflow_name,
                            file=workflow_file,
                            error_type="missing_step",
                            key=required_step,
                            message=(
                                f"Required step '{required_step}' not found in job '{job_id}'. "
                                f"此步骤在 workflow_contract.v2.json 的 required_steps 中声明，必须存在于 workflow 中。\n"
                                f"修复方法:\n"
                                f"  方案 A：在 workflow 中添加此步骤（推荐，如果步骤属于核心验证/产物生成类）\n"
                                f"  方案 B：从 contract 的 required_steps 中移除（仅当步骤确实不再需要时）\n"
                                f"  方案 C：如果步骤名称已更改，添加 alias 映射到 step_name_aliases\n"
                                f"参见 contract.md#55-required_steps-覆盖原则 了解覆盖策略"
                            ),
                            expected=required_step,
                            location=f"jobs.{job_id}.steps",
                        )
                    )
                    self.result.success = False

        # 检查 required outputs
        job_outputs = job_data.get("outputs", {})
        for required_output in required_outputs:
            if required_output not in job_outputs:
                self.result.errors.append(
                    ValidationError(
                        workflow=workflow_name,
                        file=workflow_file,
                        error_type="missing_output",
                        key=required_output,
                        message=f"Required output '{required_output}' not found in job '{job_id}'",
                        expected=required_output,
                        location=f"jobs.{job_id}.outputs.{required_output}",
                    )
                )
                self.result.success = False

        return True

    def validate_env_vars(
        self,
        workflow_name: str,
        workflow_file: str,
        workflow_data: dict[str, Any],
        required_env_vars: list[str],
    ) -> bool:
        """验证全局环境变量"""
        env = workflow_data.get("env", {})

        for required_var in required_env_vars:
            if required_var not in env:
                self.result.errors.append(
                    ValidationError(
                        workflow=workflow_name,
                        file=workflow_file,
                        error_type="missing_env_var",
                        key=required_var,
                        message=f"Required environment variable '{required_var}' not found",
                        expected=required_var,
                        location="env",
                    )
                )
                self.result.success = False

        return True

    def validate_job_ids_and_names(
        self,
        workflow_name: str,
        workflow_file: str,
        workflow_data: dict[str, Any],
        workflow_contract: dict[str, Any],
        require_job_coverage: bool = False,
    ) -> bool:
        """
        验证 job_ids 和 job_names 的一致性。

        - 校验 contract.job_ids 中的每个 id 都存在于 workflow 的 jobs 中
        - 校验 contract.job_names 与实际 jobs.<id>.name 的映射一致
        - job name 改动的容忍策略与 frozen 规则一致
        - 检测 workflow 中未在 contract.job_ids 中声明的 extra jobs

        Args:
            workflow_name: workflow 名称
            workflow_file: workflow 文件路径
            workflow_data: 解析后的 workflow 数据
            workflow_contract: workflow 合约定义
            require_job_coverage: 如果为 True，extra jobs 报告为 ERROR；否则为 WARNING

        Returns:
            bool: 校验通过返回 True
        """
        job_ids = workflow_contract.get("job_ids", [])
        job_names = workflow_contract.get("job_names", [])

        jobs = workflow_data.get("jobs", {})
        all_valid = True

        # 如果 contract 定义了 job_ids，则检测 extra jobs
        if job_ids:
            contract_job_ids = set(job_ids)
            actual_job_ids = set(jobs.keys())
            extra_job_ids = actual_job_ids - contract_job_ids

            if extra_job_ids:
                for extra_job_id in sorted(extra_job_ids):
                    extra_job_name = jobs[extra_job_id].get("name", "(unnamed)")
                    if require_job_coverage:
                        # require_job_coverage 模式：报告为 ERROR
                        self.result.errors.append(
                            ValidationError(
                                workflow=workflow_name,
                                file=workflow_file,
                                error_type="extra_job_not_in_contract",
                                key=extra_job_id,
                                message=(
                                    f"Job '{extra_job_id}' (name: '{extra_job_name}') exists in workflow "
                                    f"but is not declared in contract job_ids. "
                                    f"如需将此 job 纳入合约管理，请执行以下步骤:\n"
                                    f"  1. 更新 scripts/ci/workflow_contract.v2.json:\n"
                                    f"     - {workflow_name}.job_ids: 添加 '{extra_job_id}'\n"
                                    f"     - {workflow_name}.job_names: 添加对应的 job name\n"
                                    f"  2. 更新 docs/ci_nightly_workflow_refactor/contract.md:\n"
                                    f"     - 'Job ID 与 Job Name 对照表' 节 (contract.md#2-job-id-与-job-name-对照表)\n"
                                    f"  3. 运行 make validate-workflows 验证"
                                ),
                                expected="Job ID in contract job_ids",
                                actual=extra_job_id,
                                location=f"jobs.{extra_job_id}",
                            )
                        )
                        self.result.success = False
                        all_valid = False
                    else:
                        # 默认模式：报告为 WARNING
                        self.result.warnings.append(
                            ValidationWarning(
                                workflow=workflow_name,
                                file=workflow_file,
                                warning_type="extra_job_not_in_contract",
                                key=extra_job_id,
                                message=(
                                    f"Job '{extra_job_id}' (name: '{extra_job_name}') exists in workflow "
                                    f"but is not declared in contract job_ids"
                                ),
                                old_value="(not in contract)",
                                new_value=extra_job_id,
                                location=f"jobs.{extra_job_id}",
                            )
                        )

        if not job_ids:
            return True

        # 建立 job_id -> expected_job_name 的映射
        # job_ids 和 job_names 是位置对应的
        job_id_to_name: dict[str, str] = {}
        for i, job_id in enumerate(job_ids):
            if i < len(job_names):
                job_id_to_name[job_id] = job_names[i]

        # 校验每个 job_id 是否存在
        for job_id in job_ids:
            if job_id not in jobs:
                self.result.errors.append(
                    ValidationError(
                        workflow=workflow_name,
                        file=workflow_file,
                        error_type="missing_job_id",
                        key=job_id,
                        message=f"Required job id '{job_id}' not found in workflow jobs",
                        expected=job_id,
                        location=f"jobs.{job_id}",
                    )
                )
                self.result.success = False
                all_valid = False
                continue

            # 校验 job name 一致性
            expected_name = job_id_to_name.get(job_id)
            if expected_name:
                actual_name = jobs[job_id].get("name", "")

                if actual_name != expected_name:
                    # 检查是否属于冻结的 job name
                    is_frozen = expected_name in self.frozen_job_names

                    if is_frozen:
                        # 冻结的 job name 改名：报告为 ERROR
                        self.result.errors.append(
                            ValidationError(
                                workflow=workflow_name,
                                file=workflow_file,
                                error_type="frozen_job_name_changed",
                                key=job_id,
                                message=(
                                    f"Frozen job name '{expected_name}' was changed to '{actual_name}' "
                                    f"for job '{job_id}'. 此 job name 属于冻结文案，不能改名。如确需改名，必须满足以下最小组合:\n"
                                    f"  【必需步骤 - 缺一不可】\n"
                                    f"  1. 更新 scripts/ci/workflow_contract.v2.json:\n"
                                    f"     - frozen_job_names.allowlist: 添加新名称，移除旧名称\n"
                                    f"     - job_names[]: 同步更新对应位置\n"
                                    f"  2. 版本 bump（使用 bump_workflow_contract_version.py）:\n"
                                    f"     - python scripts/ci/bump_workflow_contract_version.py minor --message \"Rename frozen job: {expected_name} -> {actual_name}\"\n"
                                    f"  3. 更新 docs/ci_nightly_workflow_refactor/contract.md:\n"
                                    f"     - 'Job ID 与 Job Name 对照表' 节 (contract.md#2-job-id-与-job-name-对照表)\n"
                                    f"     - 'Frozen Job Names' 节 (contract.md#51-frozen-job-names)\n"
                                    f"  4. 运行 make validate-workflows-strict 验证\n"
                                    f"  详见 maintenance.md#62-冻结-step-rename-标准流程"
                                ),
                                expected=expected_name,
                                actual=actual_name,
                                location=f"jobs.{job_id}.name",
                            )
                        )
                        self.result.success = False
                        all_valid = False
                    else:
                        # 非冻结的 job name 改名：仅 WARNING
                        self.result.warnings.append(
                            ValidationWarning(
                                workflow=workflow_name,
                                file=workflow_file,
                                warning_type="job_name_mismatch",
                                key=job_id,
                                message=f"Job name mismatch for '{job_id}' (from job_ids/job_names contract)",
                                old_value=expected_name,
                                new_value=actual_name,
                                location=f"jobs.{job_id}.name",
                            )
                        )

        return all_valid

    def validate_artifact_archive(
        self,
        workflow_name: str,
        workflow_file: str,
        workflow_data: dict[str, Any],
        artifact_contract: dict[str, Any],
    ) -> bool:
        """
        验证 artifact archive 合约。

        检查 workflow 中的 actions/upload-artifact 步骤是否覆盖了
        required_artifact_paths 中定义的路径。

        Args:
            workflow_name: workflow 名称
            workflow_file: workflow 文件路径
            workflow_data: 解析后的 workflow 数据
            artifact_contract: artifact_archive 合约定义

        Returns:
            bool: 校验通过返回 True
        """
        required_paths = artifact_contract.get("required_artifact_paths", [])
        if not required_paths:
            return True

        step_name_filter = artifact_contract.get("artifact_step_names")

        # 提取所有 upload-artifact 步骤的路径
        upload_steps = extract_upload_artifact_paths(workflow_data)

        # 检查覆盖情况
        covered, missing = check_artifact_path_coverage(
            upload_steps, required_paths, step_name_filter
        )

        if missing:
            # 构建详细的错误信息
            step_filter_info = ""
            if step_name_filter:
                step_filter_info = f" (仅检查步骤: {', '.join(step_name_filter)})"

            # 收集实际上传的路径用于诊断
            actual_paths = set()
            for step in upload_steps:
                for path in step.get("paths", []):
                    actual_paths.add(path)

            for missing_path in missing:
                self.result.errors.append(
                    ValidationError(
                        workflow=workflow_name,
                        file=workflow_file,
                        error_type="missing_artifact_path",
                        key=missing_path,
                        message=(
                            f"Required artifact path '{missing_path}' is not uploaded in workflow{step_filter_info}. "
                            f"Please ensure an upload-artifact step includes this path in its 'with.path' configuration."
                        ),
                        expected=missing_path,
                        actual=f"Uploaded paths: {', '.join(sorted(actual_paths)) or '(none)'}",
                        location="artifact_archive.required_artifact_paths",
                    )
                )

            self.result.success = False
            return False

        return True

    def validate_workflow(self, workflow_name: str, workflow_contract: dict[str, Any]) -> bool:
        """验证单个 workflow"""
        workflow_file = workflow_contract["file"]
        workflow_path = self.workspace_root / workflow_file

        if not workflow_path.exists():
            self.result.errors.append(
                ValidationError(
                    workflow=workflow_name,
                    file=workflow_file,
                    error_type="workflow_not_found",
                    key="",
                    message=f"Workflow file not found: {workflow_file}",
                )
            )
            self.result.success = False
            self.result.skipped_workflows.append(workflow_name)
            return False

        workflow_data = self.load_workflow(workflow_path)
        if workflow_data is None:
            self.result.skipped_workflows.append(workflow_name)
            return False

        self.result.validated_workflows.append(workflow_name)

        # 验证 job_ids 和 job_names（如果存在）
        self.validate_job_ids_and_names(
            workflow_name,
            workflow_file,
            workflow_data,
            workflow_contract,
            require_job_coverage=self.require_job_coverage,
        )

        # 验证 required jobs
        for job_contract in workflow_contract.get("required_jobs", []):
            self.validate_job(workflow_name, workflow_file, workflow_data, job_contract)

        # 验证 required env vars
        required_env_vars = workflow_contract.get("required_env_vars", [])
        if required_env_vars:
            self.validate_env_vars(workflow_name, workflow_file, workflow_data, required_env_vars)

        # 验证 artifact archive（如果定义）
        artifact_contract = workflow_contract.get("artifact_archive")
        if artifact_contract:
            self.validate_artifact_archive(
                workflow_name, workflow_file, workflow_data, artifact_contract
            )

        return True

    def validate_contract_internal_consistency(self) -> bool:
        """
        验证 contract 内部的结构一致性。

        检查：
        1. job_ids 与 job_names 长度是否一致 (contract_job_ids_names_length_mismatch)
        2. required_jobs[*].id 是否都在 job_ids 中 (contract_required_job_not_in_job_ids)
        3. required_jobs 的 id 是否有重复 (contract_required_job_id_duplicate)
        4. job_ids 是否有重复 (contract_job_ids_duplicate)

        这些检查在 --strict 模式下会阻断 CI。

        Returns:
            bool: 一致性检查通过返回 True
        """
        all_consistent = True

        # 获取所有 workflow 定义（兼容 v1.0 和 v1.1+ 格式）
        workflows = build_workflows_view(self.contract)

        for workflow_name, workflow_def in workflows.items():
            job_ids = workflow_def.get("job_ids", [])
            job_names = workflow_def.get("job_names", [])
            required_jobs = workflow_def.get("required_jobs", [])

            # 检查 1: job_ids 与 job_names 长度是否一致
            if job_ids and job_names and len(job_ids) != len(job_names):
                self.result.errors.append(
                    ValidationError(
                        workflow=workflow_name,
                        file=str(self.contract_path),
                        error_type="contract_job_ids_names_length_mismatch",
                        key=f"{workflow_name}.job_ids/job_names",
                        message=(
                            f"Contract error: {workflow_name}.job_ids has {len(job_ids)} items, "
                            f"but {workflow_name}.job_names has {len(job_names)} items. "
                            f"These arrays must have the same length (positional mapping). "
                            f"修复方法:\n"
                            f"  1. 检查 scripts/ci/workflow_contract.v2.json 中 {workflow_name}.job_ids 和 {workflow_name}.job_names\n"
                            f"  2. 确保两个数组长度相同，且位置一一对应\n"
                            f"  3. 运行 make validate-workflows 验证"
                        ),
                        expected="job_ids.length == job_names.length",
                        actual=f"job_ids.length={len(job_ids)}, job_names.length={len(job_names)}",
                        location=f"{workflow_name}.job_ids / {workflow_name}.job_names",
                    )
                )
                self.result.success = False
                all_consistent = False

            # 检查 2: job_ids 是否有重复
            if job_ids:
                seen_job_ids: dict[str, int] = {}
                for idx, job_id in enumerate(job_ids):
                    if job_id in seen_job_ids:
                        self.result.errors.append(
                            ValidationError(
                                workflow=workflow_name,
                                file=str(self.contract_path),
                                error_type="contract_job_ids_duplicate",
                                key=job_id,
                                message=(
                                    f"Contract error: Duplicate job_id '{job_id}' in {workflow_name}.job_ids "
                                    f"(first at index {seen_job_ids[job_id]}, duplicate at index {idx}). "
                                    f"Each job_id must be unique within a workflow. "
                                    f"修复方法:\n"
                                    f"  1. 检查 scripts/ci/workflow_contract.v2.json 中 {workflow_name}.job_ids\n"
                                    f"  2. 移除重复的 job_id '{job_id}'\n"
                                    f"  3. 运行 make validate-workflows 验证"
                                ),
                                expected="unique job_id",
                                actual=f"duplicate at indices {seen_job_ids[job_id]} and {idx}",
                                location=f"{workflow_name}.job_ids[{idx}]",
                            )
                        )
                        self.result.success = False
                        all_consistent = False
                    else:
                        seen_job_ids[job_id] = idx

            # 检查 3: required_jobs 的 id 是否有重复
            if required_jobs:
                seen_required_ids: dict[str, int] = {}
                for idx, job in enumerate(required_jobs):
                    job_id = job.get("id", "")
                    if not job_id:
                        continue
                    if job_id in seen_required_ids:
                        self.result.errors.append(
                            ValidationError(
                                workflow=workflow_name,
                                file=str(self.contract_path),
                                error_type="contract_required_job_id_duplicate",
                                key=job_id,
                                message=(
                                    f"Contract error: Duplicate job id '{job_id}' in {workflow_name}.required_jobs "
                                    f"(first at index {seen_required_ids[job_id]}, duplicate at index {idx}). "
                                    f"Each required_job id must be unique. "
                                    f"修复方法:\n"
                                    f"  1. 检查 scripts/ci/workflow_contract.v2.json 中 {workflow_name}.required_jobs\n"
                                    f"  2. 移除重复的 required_job 条目 (id='{job_id}')\n"
                                    f"  3. 运行 make validate-workflows 验证"
                                ),
                                expected="unique required_job id",
                                actual=f"duplicate at indices {seen_required_ids[job_id]} and {idx}",
                                location=f"{workflow_name}.required_jobs[{idx}].id",
                            )
                        )
                        self.result.success = False
                        all_consistent = False
                    else:
                        seen_required_ids[job_id] = idx

            # 检查 4: required_jobs[*].id 是否都在 job_ids 中
            if job_ids and required_jobs:
                job_ids_set = set(job_ids)
                for idx, job in enumerate(required_jobs):
                    job_id = job.get("id", "")
                    if job_id and job_id not in job_ids_set:
                        self.result.errors.append(
                            ValidationError(
                                workflow=workflow_name,
                                file=str(self.contract_path),
                                error_type="contract_required_job_not_in_job_ids",
                                key=job_id,
                                message=(
                                    f"Contract error: required_jobs[{idx}].id='{job_id}' is not in {workflow_name}.job_ids. "
                                    f"All required_jobs ids must be declared in job_ids. "
                                    f"修复方法:\n"
                                    f"  方案 A：将 '{job_id}' 添加到 {workflow_name}.job_ids（推荐）\n"
                                    f"  方案 B：从 {workflow_name}.required_jobs 中移除此条目\n"
                                    f"参见 contract.md 第 10 章 'Extra Job Coverage 策略'"
                                ),
                                expected=f"job_id in {workflow_name}.job_ids",
                                actual=f"'{job_id}' not found in job_ids: {job_ids}",
                                location=f"{workflow_name}.required_jobs[{idx}].id",
                            )
                        )
                        self.result.success = False
                        all_consistent = False

        return all_consistent

    def validate_contract_frozen_consistency(self) -> bool:
        """
        验证 contract 内部的 frozen allowlist 一致性。

        检查：
        1. 所有 workflows.*.required_jobs[*].required_steps 是否都在 frozen_step_text.allowlist 中
        2. 所有 workflows.*.job_names 或 required_jobs[*].name 是否都在 frozen_job_names.allowlist 中

        如果发现不一致，报告 contract_frozen_step_missing 或 contract_frozen_job_missing 错误。

        Returns:
            bool: 一致性检查通过返回 True
        """
        all_consistent = True

        # 获取所有 workflow 定义（兼容 v1.0 和 v1.1+ 格式）
        workflows = build_workflows_view(self.contract)

        # 检查 1: required_steps 是否都在 frozen_step_text.allowlist 中
        for workflow_name, workflow_def in workflows.items():
            for job in workflow_def.get("required_jobs", []):
                job_id = job.get("id", "")
                for step in job.get("required_steps", []):
                    if step not in self.frozen_steps:
                        self.result.errors.append(
                            ValidationError(
                                workflow=workflow_name,
                                file=str(self.contract_path),
                                error_type="contract_frozen_step_missing",
                                key=step,
                                message=(
                                    f"Required step '{step}' in job '{job_id}' (workflow '{workflow_name}') "
                                    f"is not in frozen_step_text.allowlist. 修复方法:\n"
                                    f"  1. 如果此 step 需要冻结保护（被外部系统引用如 artifact 名称），请将其添加到 frozen_step_text.allowlist\n"
                                    f"  2. 同步更新 contract.md 'Frozen Step Names' 节 (contract.md#52-frozen-step-names)\n"
                                    f"  3. 运行 make validate-workflows 验证\n"
                                    f"注意：并非所有 required_steps 都需要冻结，参见 contract.md#55-required_steps-覆盖原则"
                                ),
                                location=f"{workflow_name}.required_jobs[{job_id}].required_steps",
                            )
                        )
                        self.result.success = False
                        all_consistent = False

        # 检查 2: job_names（或 required_jobs[*].name）是否都在 frozen_job_names.allowlist 中
        for workflow_name, workflow_def in workflows.items():
            # 方式 1: 检查 job_names 数组
            job_ids = workflow_def.get("job_ids", [])
            job_names = workflow_def.get("job_names", [])
            for idx, job_name in enumerate(job_names):
                if job_name not in self.frozen_job_names:
                    # 尝试获取对应的 job_id（通过位置对应）
                    job_id = job_ids[idx] if idx < len(job_ids) else f"index-{idx}"
                    self.result.errors.append(
                        ValidationError(
                            workflow=workflow_name,
                            file=str(self.contract_path),
                            error_type="contract_frozen_job_missing",
                            key=job_name,
                            message=(
                                f"Job name '{job_name}' (job_id: '{job_id}') in workflow '{workflow_name}' job_names "
                                f"is not in frozen_job_names.allowlist. 修复方法:\n"
                                f"  1. 如果此 job name 需要冻结保护，请将其添加到 scripts/ci/workflow_contract.v2.json 的 frozen_job_names.allowlist\n"
                                f"  2. 同步更新 contract.md 'Frozen Job Names' 节 (contract.md#51-frozen-job-names)\n"
                                f"  3. 运行 make validate-workflows 验证"
                            ),
                            location=f"{workflow_name}.job_names[{idx}]",
                        )
                    )
                    self.result.success = False
                    all_consistent = False

            # 方式 2: 检查 required_jobs[*].name
            for job in workflow_def.get("required_jobs", []):
                job_id = job.get("id", "")
                job_name = job.get("name", "")
                if job_name and job_name not in self.frozen_job_names:
                    # 避免重复报告（如果 job_names 数组中已报告过）
                    if job_name not in workflow_def.get("job_names", []):
                        self.result.errors.append(
                            ValidationError(
                                workflow=workflow_name,
                                file=str(self.contract_path),
                                error_type="contract_frozen_job_missing",
                                key=job_name,
                                message=(
                                    f"Job name '{job_name}' in required_jobs[{job_id}] (workflow '{workflow_name}') "
                                    f"is not in frozen_job_names.allowlist. 修复方法:\n"
                                    f"  1. 如果此 job name 需要冻结保护，请将其添加到 scripts/ci/workflow_contract.v2.json 的 frozen_job_names.allowlist\n"
                                    f"  2. 同步更新 contract.md 'Frozen Job Names' 节 (contract.md#51-frozen-job-names)\n"
                                    f"  3. 运行 make validate-workflows 验证"
                                ),
                                location=f"{workflow_name}.required_jobs[{job_id}].name",
                            )
                        )
                        self.result.success = False
                        all_consistent = False

        return all_consistent

    def validate(self) -> ValidationResult:
        """执行全部验证

        ============================================================================
        Phase 2 扩展点：纳入 release.yml
        ============================================================================

        本脚本使用 discover_workflow_keys() 动态发现 workflow 定义，无需硬编码。
        当 release.yml 纳入合约时，只需在 workflow_contract.v2.json 中添加 release
        字段定义即可自动被本脚本发现和校验。

        纳入 release.yml 时的同步 Checklist（本脚本无需代码修改）：

        1. [workflow_contract.v2.json] 添加 release 字段（必需）：
           - file: ".github/workflows/release.yml"
           - job_ids / job_names
           - required_jobs[].required_steps
           - artifact_archive.required_artifact_paths

        2. [workflow_contract.v2.json] 更新 frozen allowlist（如需冻结）：
           - frozen_job_names.allowlist: 添加 release 核心 job names
           - frozen_step_text.allowlist: 添加 release 核心 step names

        3. [workflow_contract.v2.json] 更新 make.targets_required（如有）：
           - 添加 release 专用 make targets（如 release-build）

        4. [本脚本] 无需修改 - 自动发现并校验 release workflow

        5. [验证] 运行以下命令确认 release 被正确校验：
           python scripts/ci/validate_workflows.py --json | jq '.validated_workflows'
           # 预期输出应包含 "release"

        详见 contract.md 2.4.3 节迁移 Checklist
        ============================================================================
        """
        if not self.load_contract():
            return self.result

        # 执行 JSON Schema 校验（如果 schema 文件存在）
        if not self.validate_schema():
            # Schema 校验失败，但继续执行其他验证以收集更多错误
            pass

        # =======================================================================
        # 校验 0: contract 内部结构一致性（job_ids/job_names 长度、重复项等）
        # 这些是 contract 自身的错误，必须首先检查
        # =======================================================================
        self.validate_contract_internal_consistency()

        # =======================================================================
        # 策略 A（最小冻结 + 核心子集 required_steps，v2.3.0+，当前默认）
        # =======================================================================
        # validate_contract_frozen_consistency() 不在默认 validate() 中调用。
        #
        # 设计意图：
        # - frozen_step_text.allowlist 和 frozen_job_names.allowlist 仅包含核心项
        #   （被外部系统引用如 artifact 名称、日志搜索，或 Required Checks 引用的 jobs）
        # - required_steps 和 job_names 不要求全部在 frozen allowlist 中
        # - 冻结项改名报 ERROR，阻止 CI
        # - 非冻结项改名仅报 WARNING，不阻止 CI
        #
        # required_steps 覆盖原则（两档策略）：
        # - 核心子集（默认）：仅纳入关键验证/产物生成步骤（如 artifact 上传、合约校验、测试运行）
        # - 全量覆盖：纳入所有步骤（适用于发布冻结期/合规审计）
        #
        # 必须纳入 required_steps 的步骤类型：
        # - 基础设置步骤（Checkout, Set up Python, Install dependencies）
        # - 合约自身校验步骤（Validate workflow contract, Check workflow contract docs sync）
        # - CI 脚本测试步骤（Run CI script tests）
        # - 核心验证/测试步骤（Run unit and integration tests, Run acceptance tests）
        # - Artifact 上传步骤（Upload test results, Upload validation report）
        #
        # 允许不纳入 required_steps 的步骤类型：
        # - 缓存步骤、诊断/调试步骤、条件执行步骤、通知步骤
        #
        # 如需策略 B（全量冻结）：
        # - 使用 --require-frozen-consistency 参数启用
        # - 会检查所有 required_steps/job_names 是否都在 frozen allowlist 中
        # - 不一致会报 ERROR
        #
        # 参见：
        # - workflow_contract.v2.json 的 _changelog_v2.3.0 说明
        # - docs/ci_nightly_workflow_refactor/contract.md 第 5 章（冻结范围）
        # - docs/ci_nightly_workflow_refactor/contract.md 第 5.5 节（required_steps 覆盖原则）
        # - docs/ci_nightly_workflow_refactor/maintenance.md 第 3.1 节

        # 获取所有 workflow 定义（兼容 v1.0 和 v1.1+ 格式）
        workflows = build_workflows_view(self.contract)

        for workflow_name, workflow_contract in workflows.items():
            self.validate_workflow(workflow_name, workflow_contract)

        # =======================================================================
        # 校验 A: contract -> Makefile
        # 检查 make.targets_required 中的 targets 是否都在 Makefile 中定义
        # =======================================================================
        self.validate_makefile_targets()

        # =======================================================================
        # 校验 B: workflow -> contract
        # 检查 workflow 中的 make 调用是否都在 make.targets_required 中声明
        # =======================================================================
        self.validate_workflow_make_calls()

        # =======================================================================
        # 校验 C: contract.ci.labels <-> gh_pr_labels_to_outputs.py LABEL_* 常量
        # 确保 PR label 定义在 contract 和脚本中保持同步
        # =======================================================================
        self.validate_ci_labels()

        return self.result

    def validate_frozen_consistency(self, strict: bool = False) -> bool:
        """
        验证 frozen allowlist 与 required_steps/required_jobs 的自一致性。

        可选检查：如果启用 strict 模式，验证所有 required_steps 是否都在
        frozen_step_text.allowlist 中，所有 required_jobs 的 name 是否都在
        frozen_job_names.allowlist 中。

        这是一个策略性检查，用于团队希望强制所有 required 项都必须冻结的场景。

        Args:
            strict: 如果为 True，则将不一致作为 ERROR 报告；否则作为 WARNING

        Returns:
            bool: 一致性检查通过返回 True
        """
        all_consistent = True

        # 收集所有 required_steps
        all_required_steps: set[str] = set()
        all_required_job_names: set[str] = set()

        # 动态发现所有 workflow keys，而非硬编码 ["ci", "nightly"]
        workflow_keys = discover_workflow_keys(self.contract)
        for workflow_key in workflow_keys:
            workflow_contract = self.contract.get(workflow_key, {})
            for job in workflow_contract.get("required_jobs", []):
                job_name = job.get("name", "")
                if job_name:
                    all_required_job_names.add(job_name)
                for step in job.get("required_steps", []):
                    all_required_steps.add(step)

        # 检查 required_steps 是否都在 frozen_step_text.allowlist 中
        unfrozen_steps = all_required_steps - self.frozen_steps
        if unfrozen_steps:
            for step in sorted(unfrozen_steps):
                if strict:
                    self.result.errors.append(
                        ValidationError(
                            workflow="",
                            file=str(self.contract_path),
                            error_type="unfrozen_required_step",
                            key=step,
                            message=(
                                f"Required step '{step}' is not in frozen_step_text.allowlist. "
                                f"如果团队策略要求所有 required_steps 必须冻结，"
                                f"请将此 step 添加到 frozen_step_text.allowlist。"
                            ),
                            location="frozen_step_text.allowlist",
                        )
                    )
                    self.result.success = False
                    all_consistent = False
                else:
                    self.result.warnings.append(
                        ValidationWarning(
                            workflow="",
                            file=str(self.contract_path),
                            warning_type="unfrozen_required_step",
                            key=step,
                            message=(
                                f"Required step '{step}' is not in frozen_step_text.allowlist. "
                                f"改名此 step 仅会产生 WARNING（非 ERROR）。"
                            ),
                            location="frozen_step_text.allowlist",
                        )
                    )

        # 检查 required_jobs 的 name 是否都在 frozen_job_names.allowlist 中
        unfrozen_jobs = all_required_job_names - self.frozen_job_names
        if unfrozen_jobs:
            for job_name in sorted(unfrozen_jobs):
                if strict:
                    self.result.errors.append(
                        ValidationError(
                            workflow="",
                            file=str(self.contract_path),
                            error_type="unfrozen_required_job",
                            key=job_name,
                            message=(
                                f"Required job name '{job_name}' is not in frozen_job_names.allowlist. "
                                f"如果团队策略要求所有 required_jobs 必须冻结，"
                                f"请将此 job name 添加到 frozen_job_names.allowlist。"
                            ),
                            location="frozen_job_names.allowlist",
                        )
                    )
                    self.result.success = False
                    all_consistent = False
                else:
                    self.result.warnings.append(
                        ValidationWarning(
                            workflow="",
                            file=str(self.contract_path),
                            warning_type="unfrozen_required_job",
                            key=job_name,
                            message=(
                                f"Required job name '{job_name}' is not in frozen_job_names.allowlist. "
                                f"改名此 job 仅会产生 WARNING（非 ERROR）。"
                            ),
                            location="frozen_job_names.allowlist",
                        )
                    )

        return all_consistent

    def _find_alias_match(self, canonical_step: str, actual_step_names: list[str]) -> Optional[str]:
        """
        检查 canonical_step 是否有 alias 匹配到 actual_step_names 中的某个步骤。

        此方法实现 Step Name 匹配优先级中的 ALIAS 级别匹配（优先级 2）。
        在 EXACT 匹配失败后、FUZZY 匹配之前调用。

        Alias 生命周期与冻结项交互
        ============================================================================

        1. **Alias 允许窗口**:
           - step_name_aliases 用于在 step 重命名的过渡期内同时接受新旧名称
           - 典型场景: workflow 中使用旧名称，contract 已更新为新名称（canonical）
           - 推荐在 2-3 个迭代周期后移除旧别名，强制更新 workflow

        2. **与冻结项的交互规则**:
           - 无论 canonical_step 是否在 frozen_step_text.allowlist 中，
             通过 alias 匹配时**始终**产生 step_name_alias_matched WARNING
           - 这是因为 alias 匹配表示 workflow 使用了"过时"的名称，应当更新

        3. **Alias vs Frozen 的设计哲学**:
           - frozen_step_text: 保护 step name 不被意外改名（CI 阻断）
           - step_name_aliases: 在改名后提供兼容窗口（非阻断）
           - 两者互补：frozen 保护关键 step，alias 辅助迁移

        4. **最佳实践**:
           - 添加 alias 时同时在 alias 列表中添加 _ttl 注释记录预期移除时间
           - 例如: "step_name_aliases": {"New Name": ["Old Name"],
                   "_ttl_Old_Name": "Remove after iteration 15"}

        文档锚点:
            - contract.md#56-step-name-aliases-别名映射
            - maintenance.md#62-冻结-step-rename-标准流程

        Args:
            canonical_step: 合约中定义的 canonical step name（required_steps 中的名称）
            actual_step_names: workflow 中实际存在的 step names 列表

        Returns:
            匹配到的 alias step name，如果没有匹配则返回 None
        """
        # 获取该 canonical step 的所有 aliases
        aliases = self.step_name_aliases.get(canonical_step, [])
        if not aliases:
            return None

        # 检查是否有 alias 在实际步骤中
        # 同分冲突处理: 返回 aliases 列表中第一个匹配到的（按配置顺序）
        for alias in aliases:
            if alias in actual_step_names:
                return alias

        return None

    def _find_fuzzy_match(self, target: str, candidates: list[str]) -> Optional[str]:
        """模糊匹配 step name

        此方法实现 Step Name 匹配优先级中的 FUZZY 级别匹配（优先级 3）。
        在 EXACT 和 ALIAS 匹配都失败后调用。

        委托给 workflow_contract_common.find_fuzzy_match() 实现。
        详细的匹配策略、阈值和同分冲突处理规则参见该函数文档。

        与冻结项的交互
        ============================================================================

        当 fuzzy 匹配成功时，根据 canonical_step 是否在 frozen_step_text 中：
        - 在 frozen_step_text 中: 产生 frozen_step_name_changed ERROR（阻断 CI）
        - 不在 frozen_step_text 中: 产生 step_name_changed WARNING（不阻断）

        这确保了冻结项的严格保护，同时允许非冻结项的灵活改名。

        文档锚点:
            - contract.md#562-匹配行为
            - contract.md#52-frozen-step-names

        Args:
            target: 要查找的目标名称（来自 contract 的 required_step）
            candidates: 候选名称列表（来自 workflow 的实际 step names）

        Returns:
            匹配到的候选名称，未匹配返回 None
        """
        return find_fuzzy_match(target, candidates)

    def validate_makefile_targets(self) -> bool:
        """
        验证 contract 中定义的 make.targets_required 是否都在 Makefile 中定义。

        校验类型 A: contract -> Makefile

        Returns:
            bool: 所有 targets 都存在返回 True
        """
        make_config = self.contract.get("make", {})
        targets_required = make_config.get("targets_required", [])

        if not targets_required:
            return True

        # 解析 Makefile
        makefile_path = self.workspace_root / "Makefile"
        makefile_targets = parse_makefile_targets(makefile_path)

        if not makefile_targets:
            self.result.errors.append(
                ValidationError(
                    workflow="",
                    file="Makefile",
                    error_type="makefile_not_found",
                    key="",
                    message="Makefile not found or empty",
                )
            )
            self.result.success = False
            return False

        all_found = True
        for target in targets_required:
            if target not in makefile_targets:
                self.result.errors.append(
                    ValidationError(
                        workflow="",
                        file="Makefile",
                        error_type="missing_makefile_target",
                        key=target,
                        message=(
                            f"ERROR: Required make target '{target}' not found in Makefile. "
                            f"If you removed this target, please update workflow_contract.v2.json "
                            f"to remove it from make.targets_required."
                        ),
                        expected=target,
                        location="make.targets_required",
                    )
                )
                self.result.success = False
                all_found = False

        return all_found

    def validate_workflow_make_calls(
        self, workflow_files: Optional[list[str]] = None, ignore_list: Optional[set[str]] = None
    ) -> bool:
        """
        验证 workflow 文件中的 make 调用是否都在 contract 的 make.targets_required 中。

        校验类型 B: workflow -> contract

        Args:
            workflow_files: 要扫描的 workflow 文件列表（相对路径），默认为 ci/nightly/release
            ignore_list: 要忽略的 target 列表（用于 make -C 或变量展开场景）

        Returns:
            bool: 所有调用的 targets 都在 contract 中返回 True
        """
        make_config = self.contract.get("make", {})
        targets_required = set(make_config.get("targets_required", []))

        if workflow_files is None:
            workflow_files = [
                ".github/workflows/ci.yml",
                ".github/workflows/nightly.yml",
                ".github/workflows/release.yml",
            ]

        if ignore_list is None:
            ignore_list = MAKE_TARGET_IGNORE_LIST

        all_valid = True

        for workflow_file in workflow_files:
            workflow_path = self.workspace_root / workflow_file
            make_calls = extract_workflow_make_calls(workflow_path)

            for call in make_calls:
                target = call.target

                # 跳过 ignore list 中的 targets
                if target in ignore_list:
                    continue

                # 检查 target 是否在 contract 的 targets_required 中
                if target not in targets_required:
                    self.result.errors.append(
                        ValidationError(
                            workflow=workflow_file,
                            file=call.workflow_file,
                            error_type="undeclared_make_target",
                            key=target,
                            message=(
                                f"ERROR: make target '{target}' called in workflow but not declared "
                                f"in workflow_contract.v2.json make.targets_required. "
                                f"Job: {call.job_id}, Step: {call.step_name or '(unnamed)'}. "
                                f"Please add '{target}' to make.targets_required or add it to the ignore list."
                            ),
                            expected="Target in make.targets_required",
                            actual=target,
                            location=f"jobs.{call.job_id}.steps",
                        )
                    )
                    self.result.success = False
                    all_valid = False

        return all_valid

    def validate_ci_labels(self) -> bool:
        """
        验证 contract.ci.labels 与 gh_pr_labels_to_outputs.py 中 LABEL_* 常量的一致性。

        校验类型 C: contract <-> script LABEL_* constants

        确保 PR label 定义在 contract 和解析脚本中保持同步。

        Returns:
            bool: 标签集合一致返回 True，不一致返回 False
        """
        # 获取 contract 中的 labels
        ci_config = self.contract.get("ci", {})
        contract_labels = set(ci_config.get("labels", []))

        if not contract_labels:
            # contract 中没有定义 labels，跳过检查
            return True

        # 解析 gh_pr_labels_to_outputs.py 中的 LABEL_* 常量
        script_path = self.workspace_root / "scripts" / "ci" / "gh_pr_labels_to_outputs.py"
        script_labels = self._parse_label_constants_from_script(script_path)

        if script_labels is None:
            # 脚本不存在或解析失败，报告警告但不阻止
            self.result.warnings.append(
                ValidationWarning(
                    workflow="ci",
                    file=str(script_path),
                    warning_type="label_script_parse_warning",
                    key="LABEL_*",
                    message=f"Could not parse LABEL_* constants from {script_path.name}. "
                    f"Skipping CI labels validation.",
                )
            )
            return True

        # 比较两个集合
        all_valid = True

        # 检查 contract 中有但脚本中没有的 labels
        missing_in_script = contract_labels - script_labels
        if missing_in_script:
            for label in sorted(missing_in_script):
                self.result.errors.append(
                    ValidationError(
                        workflow="ci",
                        file="scripts/ci/gh_pr_labels_to_outputs.py",
                        error_type="label_missing_in_script",
                        key=label,
                        message=(
                            f"ERROR: Label '{label}' is defined in contract.ci.labels but not found "
                            f"as a LABEL_* constant in gh_pr_labels_to_outputs.py. "
                            f"Please add a corresponding LABEL_* constant to the script, or remove "
                            f"the label from workflow_contract.v2.json ci.labels."
                        ),
                        expected=f"LABEL_* constant for '{label}'",
                        actual="(not found)",
                        location="ci.labels",
                    )
                )
                self.result.success = False
                all_valid = False

        # 检查脚本中有但 contract 中没有的 labels
        missing_in_contract = script_labels - contract_labels
        if missing_in_contract:
            for label in sorted(missing_in_contract):
                self.result.errors.append(
                    ValidationError(
                        workflow="ci",
                        file="scripts/ci/workflow_contract.v2.json",
                        error_type="label_missing_in_contract",
                        key=label,
                        message=(
                            f"ERROR: Label '{label}' is defined as LABEL_* constant in "
                            f"gh_pr_labels_to_outputs.py but not found in contract.ci.labels. "
                            f"Please add the label to workflow_contract.v2.json ci.labels, or remove "
                            f"the LABEL_* constant from the script."
                        ),
                        expected=f"Label '{label}' in ci.labels",
                        actual="(not found)",
                        location="scripts/ci/gh_pr_labels_to_outputs.py",
                    )
                )
                self.result.success = False
                all_valid = False

        return all_valid

    def _parse_label_constants_from_script(self, script_path: Path) -> Optional[set[str]]:
        """
        解析 Python 脚本中的 LABEL_* 常量值。

        使用 AST 解析而非 import，避免执行脚本代码。

        Args:
            script_path: 脚本文件路径

        Returns:
            LABEL_* 常量值的集合，解析失败返回 None
        """
        if not script_path.exists():
            return None

        try:
            with open(script_path, "r", encoding="utf-8") as f:
                source = f.read()

            tree = ast.parse(source)
            labels = set()

            for node in ast.walk(tree):
                # 查找形如 LABEL_XXX = "value" 的赋值语句
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id.startswith("LABEL_"):
                            # 获取赋值的值
                            if isinstance(node.value, ast.Constant) and isinstance(
                                node.value.value, str
                            ):
                                labels.add(node.value.value)

            return labels if labels else None

        except (SyntaxError, OSError):
            return None


# ============================================================================
# Output Formatters
# ============================================================================


def format_text_output(result: ValidationResult) -> str:
    """格式化文本输出"""
    lines = []

    lines.append("=" * 60)
    lines.append("Workflow Contract Validation Report")
    lines.append("=" * 60)
    lines.append("")

    # Summary
    status = "PASSED" if result.success else "FAILED"
    lines.append(f"Status: {status}")
    lines.append(f"Validated workflows: {', '.join(result.validated_workflows) or 'none'}")
    if result.skipped_workflows:
        lines.append(f"Skipped workflows: {', '.join(result.skipped_workflows)}")
    lines.append(f"Errors: {len(result.errors)}")
    lines.append(f"Warnings: {len(result.warnings)}")
    lines.append("")

    # Errors
    if result.errors:
        lines.append("-" * 60)
        lines.append("ERRORS")
        lines.append("-" * 60)
        for error in result.errors:
            lines.append("")
            lines.append(f"  [{error.error_type}] {error.workflow}:{error.file}")
            lines.append(f"  Key: {error.key}")
            lines.append(f"  Message: {error.message}")
            if error.location:
                lines.append(f"  Location: {error.location}")
            if error.expected:
                lines.append(f"  Expected: {error.expected}")
            if error.actual:
                lines.append(f"  Actual: {error.actual}")
        lines.append("")

    # Warnings
    if result.warnings:
        lines.append("-" * 60)
        lines.append("WARNINGS (Step Name Changes)")
        lines.append("-" * 60)
        for warning in result.warnings:
            lines.append("")
            lines.append(f"  [{warning.warning_type}] {warning.workflow}:{warning.file}")
            lines.append(f"  Key: {warning.key}")
            lines.append(f"  Message: {warning.message}")
            if warning.old_value and warning.new_value:
                lines.append(f"  Diff: '{warning.old_value}' -> '{warning.new_value}'")
            if warning.location:
                lines.append(f"  Location: {warning.location}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_json_output(result: ValidationResult) -> str:
    """格式化 JSON 输出"""
    output = {
        "success": result.success,
        "validated_workflows": result.validated_workflows,
        "skipped_workflows": result.skipped_workflows,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "errors": [
            {
                "workflow": e.workflow,
                "file": e.file,
                "error_type": e.error_type,
                "key": e.key,
                "message": e.message,
                "expected": e.expected,
                "actual": e.actual,
                "location": e.location,
            }
            for e in result.errors
        ],
        "warnings": [
            {
                "workflow": w.workflow,
                "file": w.file,
                "warning_type": w.warning_type,
                "key": w.key,
                "message": w.message,
                "old_value": w.old_value,
                "new_value": w.new_value,
                "location": w.location,
            }
            for w in result.warnings
        ],
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Validate GitHub Actions workflows against contract"
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("scripts/ci/workflow_contract.v2.json"),
        help="Path to contract JSON file (default: scripts/ci/workflow_contract.v2.json)",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root directory (default: current directory)",
    )
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    parser.add_argument(
        "--check-frozen-consistency",
        action="store_true",
        help="Check if all required_steps/job_names are in frozen allowlists (warning level)",
    )
    parser.add_argument(
        "--require-frozen-consistency",
        action="store_true",
        help="Require all required_steps/job_names to be in frozen allowlists (error level)",
    )
    parser.add_argument(
        "--require-job-coverage",
        action="store_true",
        help="Require all workflow jobs to be declared in contract job_ids (error level). "
        "Without this flag, extra jobs only produce warnings.",
    )

    args = parser.parse_args()

    # 解析路径
    workspace_root = args.workspace.resolve()
    contract_path = args.contract
    if not contract_path.is_absolute():
        contract_path = workspace_root / contract_path

    # 执行验证
    # --strict 模式也会启用 require_job_coverage
    require_job_coverage = args.require_job_coverage or args.strict
    validator = WorkflowContractValidator(
        contract_path, workspace_root, require_job_coverage=require_job_coverage
    )
    result = validator.validate()

    # 执行 frozen 一致性检查（如果启用）
    if args.check_frozen_consistency or args.require_frozen_consistency:
        validator.validate_frozen_consistency(strict=args.require_frozen_consistency)

    # 执行 contract 内部 frozen 一致性检查（如果启用 require-frozen-consistency）
    # 此检查验证所有 required_steps/job_names 是否都在 frozen allowlist 中
    if args.require_frozen_consistency:
        validator.validate_contract_frozen_consistency()

    # 如果 strict 模式，warnings 也算失败
    if args.strict and result.warnings:
        result.success = False

    # 输出结果
    if args.json:
        print(format_json_output(result))
    else:
        print(format_text_output(result))

    # 返回退出码
    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
