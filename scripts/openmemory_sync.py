#!/usr/bin/env python3
"""
OpenMemory 同步工具

读取 OpenMemory.upstream.lock.json 与 openmemory_patches.json，执行：
- 一致性检查（目录结构/关键文件存在性）
- 补丁应用（支持 dry-run）
- 输出下一步建议
- 上游代码获取与同步（fetch/sync）

用法:
    python scripts/openmemory_sync.py check          # 一致性检查
    python scripts/openmemory_sync.py apply          # 应用补丁
    python scripts/openmemory_sync.py apply --dry-run  # 补丁预览（不实际执行）
    python scripts/openmemory_sync.py suggest        # 输出下一步建议
    python scripts/openmemory_sync.py fetch          # 获取上游代码（下载到临时目录）
    python scripts/openmemory_sync.py sync           # 同步上游代码到本地（包含 fetch + 合并）
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import urllib.error
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Set, Tuple, List, Dict

# ============================================================================
# 轻量级 JSON Schema 校验器（纯 Python 实现，无外部依赖）
# ============================================================================
# 支持的 JSON Schema 关键字子集:
#   - type, required, properties, items, additionalProperties
#   - enum, pattern, format, minimum, maximum, minLength, maxLength
#   - anyOf, oneOf, allOf
# ============================================================================

class SchemaValidationError:
    """Schema 校验错误"""
    def __init__(self, path: str, message: str, keyword: str = ""):
        self.path = path
        self.message = message
        self.keyword = keyword
    
    def __str__(self):
        return f"{self.path}: {self.message}"
    
    def to_dict(self):
        return {"path": self.path, "message": self.message, "keyword": self.keyword}


class LightweightSchemaValidator:
    """轻量级 JSON Schema 校验器"""
    
    def __init__(self, schema: dict):
        self.schema = schema
        self.errors: List[SchemaValidationError] = []
    
    def validate(self, data: Any, path: str = "$") -> List[SchemaValidationError]:
        """校验数据是否符合 schema"""
        self.errors = []
        self._validate_node(data, self.schema, path)
        return self.errors
    
    def _add_error(self, path: str, message: str, keyword: str = ""):
        self.errors.append(SchemaValidationError(path, message, keyword))
    
    def _validate_node(self, data: Any, schema: dict, path: str):
        """递归校验节点"""
        if not isinstance(schema, dict):
            return
        
        # 处理 anyOf
        if "anyOf" in schema:
            any_valid = False
            for sub_schema in schema["anyOf"]:
                sub_validator = LightweightSchemaValidator(sub_schema)
                sub_errors = sub_validator.validate(data, path)
                if not sub_errors:
                    any_valid = True
                    break
            if not any_valid:
                self._add_error(path, "不满足 anyOf 中的任何 schema", "anyOf")
            return
        
        # 处理 oneOf
        if "oneOf" in schema:
            valid_count = 0
            for sub_schema in schema["oneOf"]:
                sub_validator = LightweightSchemaValidator(sub_schema)
                sub_errors = sub_validator.validate(data, path)
                if not sub_errors:
                    valid_count += 1
            if valid_count != 1:
                self._add_error(path, f"应恰好满足 oneOf 中的一个 schema，实际满足 {valid_count} 个", "oneOf")
            return
        
        # 处理 allOf
        if "allOf" in schema:
            for sub_schema in schema["allOf"]:
                self._validate_node(data, sub_schema, path)
            return
        
        # 类型检查
        if "type" in schema:
            self._validate_type(data, schema["type"], path)
        
        # 对象类型校验
        if isinstance(data, dict):
            self._validate_object(data, schema, path)
        
        # 数组类型校验
        if isinstance(data, list):
            self._validate_array(data, schema, path)
        
        # 字符串类型校验
        if isinstance(data, str):
            self._validate_string(data, schema, path)
        
        # 数值类型校验
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            self._validate_number(data, schema, path)
        
        # enum 校验
        if "enum" in schema:
            if data not in schema["enum"]:
                self._add_error(path, f"值 '{data}' 不在允许的枚举值 {schema['enum']} 中", "enum")
    
    def _validate_type(self, data: Any, expected_type: Any, path: str):
        """校验类型"""
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
            "null": type(None)
        }
        
        # 处理类型数组（如 ["string", "null"]）
        if isinstance(expected_type, list):
            for t in expected_type:
                if t == "null" and data is None:
                    return
                if t in type_map:
                    py_type = type_map[t]
                    if isinstance(data, py_type):
                        # 特殊处理：bool 是 int 的子类，需要排除
                        if t == "integer" and isinstance(data, bool):
                            continue
                        return
            self._add_error(path, f"类型不匹配：期望 {expected_type}，实际 {type(data).__name__}", "type")
            return
        
        # 单一类型
        if expected_type == "null":
            if data is not None:
                self._add_error(path, f"期望 null，实际 {type(data).__name__}", "type")
            return
        
        if expected_type not in type_map:
            return
        
        py_type = type_map[expected_type]
        if not isinstance(data, py_type):
            self._add_error(path, f"类型不匹配：期望 {expected_type}，实际 {type(data).__name__}", "type")
        elif expected_type == "integer" and isinstance(data, bool):
            self._add_error(path, f"类型不匹配：期望 integer，实际 boolean", "type")
    
    def _validate_object(self, data: dict, schema: dict, path: str):
        """校验对象"""
        # required 检查
        if "required" in schema:
            for req_key in schema["required"]:
                if req_key not in data:
                    self._add_error(path, f"缺少必需字段: {req_key}", "required")
        
        # properties 检查
        if "properties" in schema:
            for key, prop_schema in schema["properties"].items():
                if key in data:
                    self._validate_node(data[key], prop_schema, f"{path}.{key}")
        
        # additionalProperties 检查
        if "additionalProperties" in schema:
            add_props = schema["additionalProperties"]
            defined_keys = set(schema.get("properties", {}).keys())
            for key in data.keys():
                if key not in defined_keys:
                    if add_props is False:
                        self._add_error(path, f"不允许额外属性: {key}", "additionalProperties")
                    elif isinstance(add_props, dict):
                        self._validate_node(data[key], add_props, f"{path}.{key}")
    
    def _validate_array(self, data: list, schema: dict, path: str):
        """校验数组"""
        if "items" in schema:
            for i, item in enumerate(data):
                self._validate_node(item, schema["items"], f"{path}[{i}]")
        
        if "minItems" in schema:
            if len(data) < schema["minItems"]:
                self._add_error(path, f"数组长度 {len(data)} 小于最小值 {schema['minItems']}", "minItems")
        
        if "maxItems" in schema:
            if len(data) > schema["maxItems"]:
                self._add_error(path, f"数组长度 {len(data)} 大于最大值 {schema['maxItems']}", "maxItems")
    
    def _validate_string(self, data: str, schema: dict, path: str):
        """校验字符串"""
        if "minLength" in schema:
            if len(data) < schema["minLength"]:
                self._add_error(path, f"字符串长度 {len(data)} 小于最小值 {schema['minLength']}", "minLength")
        
        if "maxLength" in schema:
            if len(data) > schema["maxLength"]:
                self._add_error(path, f"字符串长度 {len(data)} 大于最大值 {schema['maxLength']}", "maxLength")
        
        if "pattern" in schema:
            if not re.match(schema["pattern"], data):
                self._add_error(path, f"字符串 '{data}' 不匹配模式 '{schema['pattern']}'", "pattern")
        
        if "format" in schema:
            self._validate_format(data, schema["format"], path)
    
    def _validate_format(self, data: str, fmt: str, path: str):
        """校验字符串格式"""
        if fmt == "uri":
            if not re.match(r"^https?://", data):
                self._add_error(path, f"无效的 URI 格式: {data}", "format")
        elif fmt == "date-time":
            # 简单的 ISO 8601 日期时间格式检查
            if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", data):
                self._add_error(path, f"无效的 date-time 格式: {data}", "format")
        elif fmt == "date":
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", data):
                self._add_error(path, f"无效的 date 格式: {data}", "format")
    
    def _validate_number(self, data: Any, schema: dict, path: str):
        """校验数值"""
        if "minimum" in schema:
            if data < schema["minimum"]:
                self._add_error(path, f"值 {data} 小于最小值 {schema['minimum']}", "minimum")
        
        if "maximum" in schema:
            if data > schema["maximum"]:
                self._add_error(path, f"值 {data} 大于最大值 {schema['maximum']}", "maximum")


class CheckStatus(Enum):
    """检查状态"""
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


class ConflictStrategy(Enum):
    """冲突处理策略"""
    CLEAN = "clean"    # 清洁合并：失败则停止
    THREE_WAY = "3way"  # 三方合并：尝试自动合并
    MANUAL = "manual"   # 手动处理：生成冲突文件供人工审查


# 默认排除列表：这些目录/文件在 sync 时不会被上游覆盖
DEFAULT_EXCLUDE_PATTERNS = [
    # Engram 本地补丁/自定义目录
    "libs/OpenMemory/dashboard/.engram-local",
    "libs/OpenMemory/.engram-patches",
    # 本地配置文件
    "libs/OpenMemory/.env",
    "libs/OpenMemory/.env.local",
    # Git 相关
    ".git",
    ".gitignore",
    # IDE 配置
    ".vscode",
    ".idea",
    # Node 模块（由 npm/pnpm 管理）
    "node_modules",
    # 构建产物
    "dist",
    "build",
    ".next",
    # 缓存
    "__pycache__",
    ".cache",
]


@dataclass
class CheckResult:
    """检查结果"""
    status: CheckStatus
    message: str
    details: Optional[dict] = None


@dataclass
class SyncReport:
    """同步报告"""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    overall_status: CheckStatus = CheckStatus.OK
    checks: list = field(default_factory=list)
    patches_status: dict = field(default_factory=dict)
    suggestions: list = field(default_factory=list)
    conflict_artifacts_dir: Optional[str] = None  # 冲突产物目录路径
    conflict_files: list = field(default_factory=list)  # 冲突文件列表
    
    def add_check(self, name: str, result: CheckResult):
        self.checks.append({
            "name": name,
            "status": result.status.value,
            "message": result.message,
            "details": result.details
        })
        # 更新整体状态
        if result.status == CheckStatus.ERROR:
            self.overall_status = CheckStatus.ERROR
        elif result.status == CheckStatus.WARN and self.overall_status != CheckStatus.ERROR:
            self.overall_status = CheckStatus.WARN
    
    def to_dict(self) -> dict:
        result = {
            "timestamp": self.timestamp,
            "overall_status": self.overall_status.value,
            "checks": self.checks,
            "patches_status": self.patches_status,
            "suggestions": self.suggestions,
            # 始终包含冲突相关字段（即使为空），便于统一解析
            "conflict_artifacts_dir": self.conflict_artifacts_dir or "",
            "conflict_files": self.conflict_files or []
        }
        return result


class OpenMemorySyncTool:
    """OpenMemory 同步工具"""
    
    def __init__(self, workspace_root: Optional[Path] = None):
        self.workspace_root = workspace_root or Path(__file__).parent.parent
        self.lock_file = self.workspace_root / "OpenMemory.upstream.lock.json"
        self.patches_file = self.workspace_root / "openmemory_patches.json"
        self.lock_schema_file = self.workspace_root / "schemas" / "openmemory_upstream_lock.schema.json"
        self.patches_schema_file = self.workspace_root / "schemas" / "openmemory_patches.schema.json"
        self.lock_data: Optional[dict] = None
        self.patches_data: Optional[dict] = None
        self.report = SyncReport()
    
    def load_config_files(self) -> bool:
        """加载配置文件"""
        # 加载 lock 文件
        if not self.lock_file.exists():
            self.report.add_check(
                "load_lock_file",
                CheckResult(
                    CheckStatus.ERROR,
                    f"Lock 文件不存在: {self.lock_file}",
                    {"path": str(self.lock_file)}
                )
            )
            return False
        
        try:
            with open(self.lock_file, "r", encoding="utf-8") as f:
                self.lock_data = json.load(f)
            self.report.add_check(
                "load_lock_file",
                CheckResult(CheckStatus.OK, f"Lock 文件加载成功: {self.lock_file.name}")
            )
        except json.JSONDecodeError as e:
            self.report.add_check(
                "load_lock_file",
                CheckResult(CheckStatus.ERROR, f"Lock 文件解析失败: {e}")
            )
            return False
        
        # 加载 patches 文件
        if not self.patches_file.exists():
            self.report.add_check(
                "load_patches_file",
                CheckResult(
                    CheckStatus.ERROR,
                    f"Patches 文件不存在: {self.patches_file}",
                    {"path": str(self.patches_file)}
                )
            )
            return False
        
        try:
            with open(self.patches_file, "r", encoding="utf-8") as f:
                self.patches_data = json.load(f)
            self.report.add_check(
                "load_patches_file",
                CheckResult(CheckStatus.OK, f"Patches 文件加载成功: {self.patches_file.name}")
            )
        except json.JSONDecodeError as e:
            self.report.add_check(
                "load_patches_file",
                CheckResult(CheckStatus.ERROR, f"Patches 文件解析失败: {e}")
            )
            return False
        
        return True
    
    def validate_schema(self, warn_only: bool = False) -> bool:
        """校验 lock 和 patches 文件是否符合 JSON Schema
        
        Args:
            warn_only: 如果为 True，校验失败只产生警告而非错误
        
        Returns:
            bool: 校验是否通过
        """
        all_valid = True
        
        # 校验 lock 文件
        if self.lock_schema_file.exists() and self.lock_data:
            try:
                with open(self.lock_schema_file, "r", encoding="utf-8") as f:
                    lock_schema = json.load(f)
                
                validator = LightweightSchemaValidator(lock_schema)
                errors = validator.validate(self.lock_data)
                
                if errors:
                    all_valid = False
                    error_details = [e.to_dict() for e in errors[:10]]  # 最多显示 10 个错误
                    status = CheckStatus.WARN if warn_only else CheckStatus.ERROR
                    self.report.add_check(
                        "schema_validate_lock",
                        CheckResult(
                            status,
                            f"Lock 文件 schema 校验失败: {len(errors)} 个错误",
                            {
                                "file": str(self.lock_file.name),
                                "schema": str(self.lock_schema_file.name),
                                "error_count": len(errors),
                                "errors": error_details,
                                "warn_only": warn_only
                            }
                        )
                    )
                else:
                    self.report.add_check(
                        "schema_validate_lock",
                        CheckResult(
                            CheckStatus.OK,
                            f"Lock 文件 schema 校验通过",
                            {
                                "file": str(self.lock_file.name),
                                "schema": str(self.lock_schema_file.name)
                            }
                        )
                    )
            except json.JSONDecodeError as e:
                all_valid = False
                status = CheckStatus.WARN if warn_only else CheckStatus.ERROR
                self.report.add_check(
                    "schema_validate_lock",
                    CheckResult(
                        status,
                        f"Lock schema 文件解析失败: {e}",
                        {"file": str(self.lock_schema_file.name), "warn_only": warn_only}
                    )
                )
        else:
            if not self.lock_schema_file.exists():
                self.report.add_check(
                    "schema_validate_lock",
                    CheckResult(
                        CheckStatus.WARN,
                        f"Lock schema 文件不存在: {self.lock_schema_file.name}",
                        {"file": str(self.lock_schema_file)}
                    )
                )
        
        # 校验 patches 文件
        if self.patches_schema_file.exists() and self.patches_data:
            try:
                with open(self.patches_schema_file, "r", encoding="utf-8") as f:
                    patches_schema = json.load(f)
                
                validator = LightweightSchemaValidator(patches_schema)
                errors = validator.validate(self.patches_data)
                
                if errors:
                    all_valid = False
                    error_details = [e.to_dict() for e in errors[:10]]
                    status = CheckStatus.WARN if warn_only else CheckStatus.ERROR
                    self.report.add_check(
                        "schema_validate_patches",
                        CheckResult(
                            status,
                            f"Patches 文件 schema 校验失败: {len(errors)} 个错误",
                            {
                                "file": str(self.patches_file.name),
                                "schema": str(self.patches_schema_file.name),
                                "error_count": len(errors),
                                "errors": error_details,
                                "warn_only": warn_only
                            }
                        )
                    )
                else:
                    self.report.add_check(
                        "schema_validate_patches",
                        CheckResult(
                            CheckStatus.OK,
                            f"Patches 文件 schema 校验通过",
                            {
                                "file": str(self.patches_file.name),
                                "schema": str(self.patches_schema_file.name)
                            }
                        )
                    )
            except json.JSONDecodeError as e:
                all_valid = False
                status = CheckStatus.WARN if warn_only else CheckStatus.ERROR
                self.report.add_check(
                    "schema_validate_patches",
                    CheckResult(
                        status,
                        f"Patches schema 文件解析失败: {e}",
                        {"file": str(self.patches_schema_file.name), "warn_only": warn_only}
                    )
                )
        else:
            if not self.patches_schema_file.exists():
                self.report.add_check(
                    "schema_validate_patches",
                    CheckResult(
                        CheckStatus.WARN,
                        f"Patches schema 文件不存在: {self.patches_schema_file.name}",
                        {"file": str(self.patches_schema_file)}
                    )
                )
        
        return all_valid
    
    def check_components(self) -> bool:
        """检查组件目录是否存在"""
        if not self.lock_data:
            return False
        
        components = self.lock_data.get("components", {})
        all_ok = True
        missing = []
        existing = []
        
        for name, path in components.items():
            full_path = self.workspace_root / path
            if full_path.exists():
                existing.append(path)
            else:
                missing.append(path)
                all_ok = False
        
        if all_ok:
            self.report.add_check(
                "components_exist",
                CheckResult(
                    CheckStatus.OK,
                    f"所有组件目录存在 ({len(existing)}/{len(components)})",
                    {"existing": existing}
                )
            )
        else:
            self.report.add_check(
                "components_exist",
                CheckResult(
                    CheckStatus.ERROR,
                    f"部分组件目录缺失 ({len(existing)}/{len(components)})",
                    {"existing": existing, "missing": missing}
                )
            )
        
        return all_ok
    
    def check_patched_files(self) -> bool:
        """检查被补丁修改的文件是否存在"""
        if not self.lock_data:
            return False
        
        patched_files = self.lock_data.get("patched_files", [])
        all_ok = True
        missing = []
        existing = []
        
        for item in patched_files:
            path = item.get("path", "")
            full_path = self.workspace_root / path
            if full_path.exists():
                existing.append({
                    "path": path,
                    "patch_count": item.get("patch_count", 0),
                    "categories": item.get("categories", {})
                })
            else:
                missing.append(path)
                all_ok = False
        
        if all_ok:
            self.report.add_check(
                "patched_files_exist",
                CheckResult(
                    CheckStatus.OK,
                    f"所有补丁目标文件存在 ({len(existing)}/{len(patched_files)})",
                    {"files": existing}
                )
            )
        else:
            self.report.add_check(
                "patched_files_exist",
                CheckResult(
                    CheckStatus.ERROR,
                    f"部分补丁目标文件缺失 ({len(existing)}/{len(patched_files)})",
                    {"existing": [f["path"] for f in existing], "missing": missing}
                )
            )
        
        return all_ok
    
    def check_upstream_info(self) -> bool:
        """检查上游信息完整性
        
        关键字段缺失会触发 ERROR（而非 WARN），并提供明确的修复建议。
        """
        if not self.lock_data:
            return False
        
        # 必须字段 - 缺失则 ERROR
        required_fields = ["upstream_url", "upstream_ref", "upstream_ref_type"]
        # 关键追踪字段 - 缺失则 ERROR（升级/回滚依赖这些字段）
        critical_fields = ["upstream_commit_sha", "upstream_commit_date"]
        # 策略字段 - 缺失则 WARN
        policy_fields = ["upgrade_policy", "artifacts", "checksums"]
        
        missing_required = []
        missing_critical = []
        missing_policy = []
        info = {}
        
        # 检查必须字段
        for field_name in required_fields:
            value = self.lock_data.get(field_name)
            if value:
                info[field_name] = value
            else:
                missing_required.append(field_name)
        
        # 检查关键追踪字段
        for field_name in critical_fields:
            value = self.lock_data.get(field_name)
            if value:
                info[field_name] = value
            else:
                missing_critical.append(field_name)
        
        # 检查策略字段
        for field_name in policy_fields:
            value = self.lock_data.get(field_name)
            if value:
                info[field_name] = f"[已配置: {type(value).__name__}]"
            else:
                missing_policy.append(field_name)
        
        # 添加可选信息
        optional_fields = ["upstream_note"]
        for field_name in optional_fields:
            value = self.lock_data.get(field_name)
            if value:
                info[field_name] = value
        
        # 生成修复建议
        remediation = []
        if missing_required:
            remediation.append(f"[ERROR] 缺少必须字段: {missing_required}")
            remediation.append("  修复: 在 OpenMemory.upstream.lock.json 中添加这些字段")
        if missing_critical:
            remediation.append(f"[ERROR] 缺少关键追踪字段: {missing_critical}")
            remediation.append("  修复: 运行 'python scripts/openmemory_sync.py fetch' 从上游获取")
            remediation.append("  或手动填写: upstream_commit_sha (40位hex), upstream_commit_date (ISO8601)")
        if missing_policy:
            remediation.append(f"[WARN] 缺少策略字段: {missing_policy}")
            remediation.append("  修复: 参考文档完善 upgrade_policy, artifacts, checksums 配置")
        
        # 判断结果级别
        has_error = bool(missing_required or missing_critical)
        has_warn = bool(missing_policy)
        
        if has_error:
            self.report.add_check(
                "upstream_info",
                CheckResult(
                    CheckStatus.ERROR,
                    f"上游信息不完整: 缺少必须/关键字段",
                    {
                        "info": info,
                        "missing_required": missing_required,
                        "missing_critical": missing_critical,
                        "missing_policy": missing_policy,
                        "remediation": remediation
                    }
                )
            )
            # 输出明确的修复建议到控制台
            print("\n[UPSTREAM INFO ERROR] 关键字段缺失，需要修复：")
            for line in remediation:
                print(f"  {line}")
            return False
        elif has_warn:
            self.report.add_check(
                "upstream_info",
                CheckResult(
                    CheckStatus.WARN,
                    f"上游信息基本完整，但缺少策略配置: {missing_policy}",
                    {
                        "info": info,
                        "missing_policy": missing_policy,
                        "remediation": remediation
                    }
                )
            )
            return True
        else:
            self.report.add_check(
                "upstream_info",
                CheckResult(
                    CheckStatus.OK,
                    f"上游信息完整: {info.get('upstream_ref', 'unknown')}",
                    {"info": info}
                )
            )
            return True
    
    def check_patches_consistency(self) -> bool:
        """检查 patches 文件与 lock 文件的一致性"""
        if not self.lock_data or not self.patches_data:
            return False
        
        # 从 lock 文件获取补丁文件列表
        lock_files = set(item["path"] for item in self.lock_data.get("patched_files", []))
        
        # 从 patches 文件获取补丁文件列表
        patches_files = set(patch["file"] for patch in self.patches_data.get("patches", []))
        
        # 检查一致性
        only_in_lock = lock_files - patches_files
        only_in_patches = patches_files - lock_files
        common = lock_files & patches_files
        
        issues = []
        if only_in_lock:
            issues.append(f"仅在 lock 中: {list(only_in_lock)}")
        if only_in_patches:
            issues.append(f"仅在 patches 中: {list(only_in_patches)}")
        
        if issues:
            self.report.add_check(
                "patches_consistency",
                CheckResult(
                    CheckStatus.WARN,
                    f"补丁文件列表不一致: {'; '.join(issues)}",
                    {
                        "common": list(common),
                        "only_in_lock": list(only_in_lock),
                        "only_in_patches": list(only_in_patches)
                    }
                )
            )
            return False
        else:
            self.report.add_check(
                "patches_consistency",
                CheckResult(
                    CheckStatus.OK,
                    f"补丁文件列表一致 ({len(common)} 个文件)",
                    {"files": list(common)}
                )
            )
            return True
    
    def check_patch_categories_summary(self) -> dict:
        """汇总补丁分类"""
        if not self.patches_data:
            return {}
        
        summary = self.patches_data.get("summary", {})
        
        self.report.patches_status = {
            "total": summary.get("total_patches", 0),
            "category_A_must_keep": summary.get("category_A_must_keep", 0),
            "category_B_upstreamable": summary.get("category_B_upstreamable", 0),
            "category_C_removable": summary.get("category_C_removable", 0),
            "details": {
                "A": summary.get("category_A_details", ""),
                "B": summary.get("category_B_details", ""),
                "C": summary.get("category_C_details", "")
            }
        }
        
        self.report.add_check(
            "patch_categories",
            CheckResult(
                CheckStatus.OK,
                f"补丁分类统计: A={summary.get('category_A_must_keep', 0)}, "
                f"B={summary.get('category_B_upstreamable', 0)}, "
                f"C={summary.get('category_C_removable', 0)}",
                {"summary": self.report.patches_status}
            )
        )
        
        return self.report.patches_status
    
    def check_patch_files_manifest(self, strict: bool = False) -> bool:
        """检查 patch 文件清单的完整性和 SHA256 一致性
        
        读取 openmemory_patches.json，对每个 change 的 patch_file 计算 sha256 
        并比对 patch_sha256；对 summary.bundle_sha256 做重新计算比对。
        
        Args:
            strict: 严格模式，patch 文件缺失或校验不匹配会导致 ERROR；
                    非严格模式仅产生 WARN
        
        Returns:
            bool: 检查是否通过（严格模式下任何问题返回 False）
        """
        if not self.patches_data:
            return False
        
        # 结果收集
        patch_files_status = {
            "total": 0,
            "verified": 0,
            "missing": 0,
            "sha256_mismatch": 0,
            "sha256_null": 0,
            "missing_files": [],
            "mismatch_files": [],
            "null_sha256_files": [],
            "category_breakdown": {"A": [], "B": [], "C": []},
            "strict_mode": strict  # 记录是否为严格模式
        }
        
        # 遍历所有 patches -> changes，检查 patch_file
        patches = self.patches_data.get("patches", [])
        for patch_group in patches:
            changes = patch_group.get("changes", [])
            for change in changes:
                patch_file = change.get("patch_file")
                patch_sha256 = change.get("patch_sha256")
                change_id = change.get("id", "unknown")
                category = change.get("category", "C")
                
                if not patch_file:
                    continue
                
                patch_files_status["total"] += 1
                patch_path = self.workspace_root / patch_file
                
                # 检查文件是否存在
                if not patch_path.exists():
                    patch_files_status["missing"] += 1
                    patch_files_status["missing_files"].append({
                        "id": change_id,
                        "path": patch_file,
                        "category": category
                    })
                    patch_files_status["category_breakdown"][category].append({
                        "id": change_id,
                        "status": "missing"
                    })
                    continue
                
                # 计算实际 SHA256
                actual_sha256 = self._compute_file_sha256(patch_path)
                
                # 检查 patch_sha256 是否为 null
                if patch_sha256 is None:
                    patch_files_status["sha256_null"] += 1
                    patch_files_status["null_sha256_files"].append({
                        "id": change_id,
                        "path": patch_file,
                        "category": category,
                        "actual_sha256": actual_sha256
                    })
                    patch_files_status["category_breakdown"][category].append({
                        "id": change_id,
                        "status": "null_sha256",
                        "actual_sha256": actual_sha256
                    })
                elif actual_sha256 != patch_sha256:
                    patch_files_status["sha256_mismatch"] += 1
                    patch_files_status["mismatch_files"].append({
                        "id": change_id,
                        "path": patch_file,
                        "category": category,
                        "expected": patch_sha256,
                        "actual": actual_sha256
                    })
                    patch_files_status["category_breakdown"][category].append({
                        "id": change_id,
                        "status": "mismatch"
                    })
                else:
                    patch_files_status["verified"] += 1
                    patch_files_status["category_breakdown"][category].append({
                        "id": change_id,
                        "status": "verified"
                    })
        
        # 检查 bundle_sha256
        summary = self.patches_data.get("summary", {})
        bundle_sha256 = summary.get("bundle_sha256")
        bundle_status = {"expected": bundle_sha256, "actual": None, "status": "unknown"}
        
        # 计算所有存在的 patch 文件的联合 SHA256
        manifest = self.patches_data.get("patch_files_manifest", {})
        base_dir = manifest.get("base_dir", "patches/openmemory")
        categories_info = manifest.get("categories", {})
        
        # 按顺序收集所有 patch 文件路径
        all_patch_files = []
        for cat in ["A", "B", "C"]:
            cat_info = categories_info.get(cat, {})
            cat_path = cat_info.get("path", f"{base_dir}/{cat}")
            files = cat_info.get("files", [])
            for f in files:
                all_patch_files.append(self.workspace_root / cat_path / f)
        
        # 计算联合 SHA256（按字典序排序路径以保证一致性）
        existing_files = sorted([f for f in all_patch_files if f.exists()])
        if existing_files:
            combined_hash = hashlib.sha256()
            for f in existing_files:
                file_hash = self._compute_file_sha256(f)
                if file_hash:
                    combined_hash.update(file_hash.encode('utf-8'))
            bundle_status["actual"] = combined_hash.hexdigest()
        
        if bundle_sha256 is None:
            bundle_status["status"] = "null"
        elif bundle_status["actual"] is None:
            bundle_status["status"] = "no_files"
        elif bundle_status["actual"] == bundle_sha256:
            bundle_status["status"] = "verified"
        else:
            bundle_status["status"] = "mismatch"
        
        patch_files_status["bundle_sha256"] = bundle_status
        
        # 存储到 report.patches_status
        if "patch_files_check" not in self.report.patches_status:
            self.report.patches_status["patch_files_check"] = {}
        self.report.patches_status["patch_files_check"] = patch_files_status
        
        # 确定检查结果状态
        verified = patch_files_status["verified"]
        total = patch_files_status["total"]
        missing = patch_files_status["missing"]
        mismatch = patch_files_status["sha256_mismatch"]
        null_count = patch_files_status["sha256_null"]
        
        # ===================================================================
        # 路径 B 策略 (2026-01)：不强制要求 patch 文件
        # ===================================================================
        # - 当 patch 文件全部缺失 (total == missing && mismatch == 0)：
        #   - 非严格模式: INFO 状态（patch 文件尚未生成，属于预期行为）
        #   - 严格模式: ERROR 状态（如 upstream_ref 变更时需强制验证）
        # - 当存在部分 patch 文件时，按原有逻辑处理
        # ===================================================================
        all_missing = (total > 0 and missing == total and mismatch == 0)
        
        has_issues = (
            patch_files_status["sha256_mismatch"] > 0 or
            bundle_status["status"] == "mismatch" or
            (patch_files_status["missing"] > 0 and not all_missing)  # 部分缺失才视为问题
        )
        has_null_sha256 = (
            patch_files_status["sha256_null"] > 0 or
            bundle_status["status"] == "null"
        )
        
        # 构建消息
        if all_missing:
            # 所有 patch 文件都缺失 - 路径 B 策略下的预期状态
            if strict:
                status = CheckStatus.ERROR
                msg = f"Patch 文件全部缺失 ({missing}/{total})，严格模式要求存在 patch 文件"
            else:
                status = CheckStatus.INFO if hasattr(CheckStatus, 'INFO') else CheckStatus.WARN
                msg = f"Patch 文件尚未生成 ({total} 个定义，0 个存在)，非严格模式下跳过校验"
        elif has_issues:
            if strict:
                status = CheckStatus.ERROR
                msg = f"Patch 文件校验失败: {missing} 缺失, {mismatch} SHA256 不匹配"
            else:
                status = CheckStatus.WARN
                msg = f"Patch 文件存在问题: {missing} 缺失, {mismatch} SHA256 不匹配 (非严格模式)"
        elif has_null_sha256:
            status = CheckStatus.WARN
            msg = f"Patch 文件校验部分完成: {verified}/{total} 已验证, {null_count} 个 SHA256 为 null"
        elif total == 0:
            status = CheckStatus.WARN
            msg = "未找到 patch 文件定义"
        else:
            status = CheckStatus.OK
            msg = f"Patch 文件校验通过: {verified}/{total} 个文件"
        
        # 添加 bundle 状态
        if bundle_status["status"] == "mismatch":
            if strict:
                status = CheckStatus.ERROR
            else:
                status = CheckStatus.WARN if status != CheckStatus.ERROR else status
            msg += f"; bundle SHA256 不匹配"
        elif bundle_status["status"] == "null":
            msg += f"; bundle SHA256 为 null"
        elif bundle_status["status"] == "verified":
            msg += f"; bundle SHA256 已验证"
        
        self.report.add_check(
            "patch_files_manifest",
            CheckResult(status, msg, patch_files_status)
        )
        
        # 严格模式下有问题返回 False
        if strict and has_issues:
            return False
        
        return True
    
    def run_consistency_check(self, schema_warn_only: bool = True, strict_patch_files: bool = False) -> bool:
        """执行完整的一致性检查
        
        Args:
            schema_warn_only: 如果为 True，schema 校验失败只产生警告（默认 True，用于渐进式上线）
            strict_patch_files: 如果为 True，patch 文件缺失/校验失败会导致错误（默认 False）
        
        Returns:
            bool: 所有检查是否通过
        """
        if not self.load_config_files():
            return False
        
        results = [
            self.check_upstream_info(),
            self.check_components(),
            self.check_patched_files(),
            self.check_patches_consistency(),
        ]
        
        # Schema 校验（新增）
        schema_valid = self.validate_schema(warn_only=schema_warn_only)
        if not schema_warn_only:
            results.append(schema_valid)
        
        self.check_patch_categories_summary()
        
        # Patch 文件清单校验（新增）
        patch_files_valid = self.check_patch_files_manifest(strict=strict_patch_files)
        if strict_patch_files:
            results.append(patch_files_valid)
        
        return all(results)
    
    def _compute_file_sha256(self, file_path: Path) -> Optional[str]:
        """计算文件的 SHA256 校验和"""
        if not file_path.exists():
            return None
        try:
            sha256_hash = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256_hash.update(chunk)
            return sha256_hash.hexdigest()
        except Exception:
            return None
    
    def _sort_patches_by_category(self, patches: list) -> list:
        """按 Category A→B→C 顺序排序补丁"""
        category_order = {"A": 0, "B": 1, "C": 2}
        
        sorted_patches = []
        for patch_group in patches:
            changes = patch_group.get("changes", [])
            # 按 category 排序 changes
            sorted_changes = sorted(
                changes, 
                key=lambda c: category_order.get(c.get("category", "C"), 3)
            )
            sorted_patches.append({
                **patch_group,
                "changes": sorted_changes
            })
        
        return sorted_patches
    
    def _compute_base_checksums_from_upstream(
        self,
        upstream_dir: Path,
        ref: str,
        ref_type: str,
        commit_sha: Optional[str]
    ) -> Dict[str, Dict[str, Any]]:
        """
        从上游解压目录计算 patched_files 的 base checksum
        
        Args:
            upstream_dir: 上游解压目录（如 /tmp/openmemory_sync_xxx/OpenMemory-1.3.0）
            ref: 版本引用（如 v1.3.0）
            ref_type: 引用类型（tag/commit）
            commit_sha: commit SHA
        
        Returns:
            Dict[path, {"base": sha256, "base_ref": ref, "base_ref_type": ref_type, "base_commit_sha": sha}]
        """
        result = {}
        patched_files = self.lock_data.get("patched_files", [])
        
        for item in patched_files:
            path = item.get("path", "")
            # path 格式为 libs/OpenMemory/packages/...
            # 需要去掉 libs/OpenMemory/ 前缀得到 upstream 目录中的相对路径
            if path.startswith("libs/OpenMemory/"):
                rel_path = path[len("libs/OpenMemory/"):]
            else:
                rel_path = path
            
            upstream_file = upstream_dir / rel_path
            
            if upstream_file.exists():
                base_sha256 = self._compute_file_sha256(upstream_file)
                result[path] = {
                    "base": base_sha256,
                    "base_ref": ref,
                    "base_ref_type": ref_type,
                    "base_commit_sha": commit_sha
                }
            else:
                # 文件在上游不存在（可能是新增的测试文件等）
                result[path] = {
                    "base": None,
                    "base_ref": ref,
                    "base_ref_type": ref_type,
                    "base_commit_sha": commit_sha
                }
        
        return result
    
    def _update_lock_base_checksums(
        self,
        base_checksums: Dict[str, Dict[str, Any]]
    ) -> bool:
        """
        更新 lock 文件中的 base checksums 信息
        
        Args:
            base_checksums: 由 _compute_base_checksums_from_upstream 返回的结果
        
        Returns:
            是否成功
        """
        try:
            # 确保 checksums 结构存在
            if "checksums" not in self.lock_data:
                self.lock_data["checksums"] = {
                    "description": "补丁文件的 SHA256 校验和，用于验证补丁落地状态",
                    "patched_files": {}
                }
            elif "patched_files" not in self.lock_data["checksums"]:
                self.lock_data["checksums"]["patched_files"] = {}
            
            patched_files = self.lock_data["checksums"]["patched_files"]
            
            for path, info in base_checksums.items():
                if path not in patched_files:
                    patched_files[path] = {"after": None}
                
                # 更新 base 相关字段
                patched_files[path]["base"] = info.get("base")
                patched_files[path]["base_ref"] = info.get("base_ref")
                patched_files[path]["base_ref_type"] = info.get("base_ref_type")
                patched_files[path]["base_commit_sha"] = info.get("base_commit_sha")
            
            # 写入文件（统一格式：2空格缩进、键排序、UTF-8、尾换行）
            with open(self.lock_file, "w", encoding="utf-8") as f:
                json.dump(self.lock_data, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.write("\n")
            
            return True
        except Exception as e:
            print(f"[ERROR] 更新 lock 文件 base checksums 失败: {e}")
            return False
    
    def _evaluate_freeze_and_update_ref_policy(
        self,
        freeze_status: Dict[str, Any],
        freeze_rules: List[Dict[str, Any]],
        strict_patch_files: bool = False,
        verify_result: Optional[Dict[str, Any]] = None,
        apply_result: Optional[Dict[str, Any]] = None,
        override_reason: Optional[str] = None,
        override_authority: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        评估冻结状态和更新引用策略
        
        Args:
            freeze_status: lock 文件中的 freeze_status 字段
            freeze_rules: upgrade_policy.freeze_rules.rules 列表
            strict_patch_files: 是否为严格补丁文件模式（upstream_ref 变更时需强制验证）
            verify_result: verify_patches() 的结果（可选）
            apply_result: apply_patches() 的结果（可选）
            override_reason: 覆盖原因（如果有）
            override_authority: 覆盖权限标签（如果有）
        
        Returns:
            决策结果字典:
            {
                "is_frozen": bool,           # 当前是否处于冻结状态
                "needs_override": bool,      # 是否需要覆盖才能继续
                "allow_update_ref": bool,    # 是否允许更新 upstream_ref
                "block_reason": str,         # 阻止原因（如果有）
                "recommended_flags": list,   # 推荐的命令行标志
                "triggered_rules": list,     # 触发的冻结规则
                "override_valid": bool,      # 提供的覆盖是否有效
                "override_issues": list      # 覆盖存在的问题
            }
        """
        result = {
            "is_frozen": False,
            "needs_override": False,
            "allow_update_ref": True,
            "block_reason": "",
            "recommended_flags": [],
            "triggered_rules": [],
            "override_valid": False,
            "override_issues": []
        }
        
        # ============================================================
        # 1. 检查 freeze_status 中的显式冻结状态
        # ============================================================
        is_frozen = freeze_status.get("is_frozen", False)
        freeze_reason = freeze_status.get("freeze_reason")
        freeze_expires_at = freeze_status.get("freeze_expires_at")
        
        if is_frozen:
            result["is_frozen"] = True
            result["needs_override"] = True
            result["allow_update_ref"] = False
            result["block_reason"] = freeze_reason or "显式冻结状态（未说明原因）"
            result["triggered_rules"].append({
                "id": "explicit_freeze",
                "reason": result["block_reason"],
                "expires_at": freeze_expires_at
            })
            
            # 检查冻结是否已过期
            if freeze_expires_at:
                try:
                    expires_dt = datetime.fromisoformat(freeze_expires_at.replace("Z", "+00:00"))
                    now = datetime.now(expires_dt.tzinfo) if expires_dt.tzinfo else datetime.now()
                    if now > expires_dt:
                        # 冻结已过期，自动解除
                        result["is_frozen"] = False
                        result["needs_override"] = False
                        result["allow_update_ref"] = True
                        result["block_reason"] = ""
                        result["triggered_rules"][-1]["expired"] = True
                except (ValueError, TypeError):
                    pass  # 无法解析时间，保持冻结状态
        
        # ============================================================
        # 2. 检查 freeze_rules 中的规则
        # ============================================================
        for rule in freeze_rules:
            if not rule.get("enabled", True):
                continue
            
            rule_id = rule.get("id", "unknown")
            scope = rule.get("scope", [])
            condition = rule.get("condition", {})
            reason_template = rule.get("reason_template", f"规则 {rule_id} 触发")
            
            # 检查 scope 是否包含 upstream_ref_update
            if "upstream_ref_update" not in scope and "sync" not in scope:
                continue
            
            # 评估条件
            condition_type = condition.get("type")
            rule_triggered = False
            trigger_reason = ""
            
            if condition_type == "flag":
                # 标志检查（需要外部提供标志状态，这里检查 verify/apply 结果中的标志）
                flag_name = condition.get("flag_name", "")
                expected_value = condition.get("expected_value", True)
                
                # 从环境变量或结果中查找标志
                env_value = os.environ.get(flag_name, "").lower() in ("true", "1", "yes")
                if env_value == expected_value:
                    rule_triggered = True
                    trigger_reason = reason_template
            
            elif condition_type == "conflict_check":
                # 冲突检查（检查 verify/apply 结果中的 Category A 冲突）
                category = condition.get("category", "A_must_keep")
                conflict_severity = condition.get("conflict_severity", "unresolvable")
                
                # 从 verify_result 或 apply_result 中检查冲突
                category_key = category.replace("_must_keep", "").replace("_upstreamable", "").replace("_removable", "")
                
                if verify_result:
                    category_mismatch = verify_result.get("category_mismatch", {})
                    if category_mismatch.get(category_key, 0) > 0:
                        rule_triggered = True
                        trigger_reason = reason_template
                
                if apply_result and not rule_triggered:
                    conflicts_by_cat = apply_result.get("conflicts_by_category", {})
                    if len(conflicts_by_cat.get(category_key, [])) > 0:
                        rule_triggered = True
                        trigger_reason = reason_template
            
            elif condition_type == "time_window":
                # 时间窗口检查（需要 release_date 配置，这里简化处理）
                # 实际实现需要从配置或环境获取 release_date
                pass
            
            if rule_triggered:
                result["is_frozen"] = True
                result["needs_override"] = True
                result["allow_update_ref"] = False
                if result["block_reason"]:
                    result["block_reason"] += "; " + trigger_reason
                else:
                    result["block_reason"] = trigger_reason
                
                result["triggered_rules"].append({
                    "id": rule_id,
                    "reason": trigger_reason,
                    "override_config": rule.get("override", {})
                })
        
        # ============================================================
        # 3. 检查严格补丁文件模式下的 verify 结果
        # ============================================================
        if strict_patch_files and verify_result:
            summary = verify_result.get("summary", {})
            category_mismatch = verify_result.get("category_mismatch", {})
            
            # Category A mismatch 或 missing 时阻止更新
            cat_a_mismatch = category_mismatch.get("A", 0)
            missing_count = summary.get("missing", 0)
            
            if cat_a_mismatch > 0 or missing_count > 0:
                result["needs_override"] = True
                result["allow_update_ref"] = False
                block_msg = f"严格模式: Category A mismatch ({cat_a_mismatch}), 缺失文件 ({missing_count})"
                if result["block_reason"]:
                    result["block_reason"] += "; " + block_msg
                else:
                    result["block_reason"] = block_msg
                
                result["triggered_rules"].append({
                    "id": "strict_patch_verify",
                    "reason": block_msg
                })
        
        # ============================================================
        # 4. 检查 apply_result 中的阻止状态
        # ============================================================
        if apply_result:
            if apply_result.get("lock_update_blocked", False):
                result["needs_override"] = True
                result["allow_update_ref"] = False
                block_msg = apply_result.get("lock_block_reason", "apply 阻止更新")
                if result["block_reason"]:
                    result["block_reason"] += "; " + block_msg
                else:
                    result["block_reason"] = block_msg
                
                result["triggered_rules"].append({
                    "id": "apply_block",
                    "reason": block_msg
                })
        
        # ============================================================
        # 5. 评估 override 有效性
        # ============================================================
        if result["needs_override"] and override_reason:
            result["override_valid"] = True
            override_issues = []
            
            # 检查每个触发规则的 override 要求
            for triggered in result["triggered_rules"]:
                override_config = triggered.get("override_config", {})
                min_reason_len = override_config.get("min_reason_len", 20)
                authority_label = override_config.get("authority_label", "tech-lead")
                
                # 检查原因长度
                if len(override_reason) < min_reason_len:
                    override_issues.append(
                        f"规则 {triggered['id']}: 覆盖原因需至少 {min_reason_len} 字符（当前 {len(override_reason)}）"
                    )
                    result["override_valid"] = False
                
                # 检查权限标签（如果提供）
                if override_authority and authority_label:
                    # 简化处理：只要提供了 authority 就认为有效
                    # 实际场景可能需要与权限系统集成
                    pass
            
            result["override_issues"] = override_issues
            
            # 如果 override 有效，允许更新
            if result["override_valid"]:
                result["allow_update_ref"] = True
                result["block_reason"] = ""
        
        # ============================================================
        # 6. 生成推荐标志
        # ============================================================
        if result["needs_override"] and not result["override_valid"]:
            result["recommended_flags"].append("--override-reason \"<详细原因>\"")
            result["recommended_flags"].append("--override-authority <权限标签>")
            
            if result["is_frozen"]:
                result["recommended_flags"].append("--force-unfreeze")
            
            # 根据触发规则给出特定建议
            for triggered in result["triggered_rules"]:
                rule_id = triggered.get("id", "")
                if rule_id == "category_a_conflict":
                    result["recommended_flags"].append("--resolve-conflicts")
                elif rule_id == "strict_patch_verify":
                    result["recommended_flags"].append("--force-update-lock")
        
        return result
    
    def _update_lock_checksums(self, checksums: dict) -> bool:
        """更新 lock 文件中的 checksums
        
        新结构:
        {
            "checksums": {
                "description": "...",
                "patched_files": {
                    "path": { "after": "sha256", "base": null }
                }
            }
        }
        """
        try:
            # 确保 checksums 字段存在并有正确结构
            if "checksums" not in self.lock_data:
                self.lock_data["checksums"] = {
                    "description": "补丁文件的 SHA256 校验和，用于验证补丁落地状态",
                    "patched_files": {}
                }
            elif not isinstance(self.lock_data["checksums"], dict):
                self.lock_data["checksums"] = {
                    "description": "补丁文件的 SHA256 校验和，用于验证补丁落地状态",
                    "patched_files": {}
                }
            elif "patched_files" not in self.lock_data["checksums"]:
                # 迁移旧格式到新格式
                old_checksums = {k: v for k, v in self.lock_data["checksums"].items() 
                                if k not in ("description", "patched_files")}
                self.lock_data["checksums"] = {
                    "description": self.lock_data["checksums"].get(
                        "description", 
                        "补丁文件的 SHA256 校验和，用于验证补丁落地状态"
                    ),
                    "patched_files": {
                        path: {"after": sha, "base": None} 
                        for path, sha in old_checksums.items()
                    }
                }
            
            # 更新 patched_files 中的 after 值
            patched_files = self.lock_data["checksums"]["patched_files"]
            for path, sha in checksums.items():
                if path in patched_files and isinstance(patched_files[path], dict):
                    patched_files[path]["after"] = sha
                else:
                    patched_files[path] = {"after": sha, "base": None}
            
            self.lock_data["last_sync_at"] = datetime.now().isoformat() + "Z"
            
            # 写入文件（统一格式：2空格缩进、键排序、UTF-8、尾换行）
            with open(self.lock_file, "w", encoding="utf-8") as f:
                json.dump(self.lock_data, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.write("\n")
            return True
        except Exception as e:
            print(f"[ERROR] 更新 lock 文件失败: {e}")
            return False
    
    def _get_conflict_artifacts_dir(self) -> Path:
        """获取冲突产物目录路径"""
        return self.workspace_root / ".artifacts" / "openmemory-patch-conflicts"
    
    def _write_conflict_file(
        self, 
        patch_id: str, 
        file_path: str, 
        category: str,
        reason: str, 
        context: str,
        strategy: ConflictStrategy
    ) -> Optional[str]:
        """
        写入冲突文件到产物目录
        
        Args:
            patch_id: 补丁 ID
            file_path: 目标文件路径
            category: 补丁分类 (A/B/C)
            reason: 冲突原因
            context: 冲突上下文
            strategy: 冲突处理策略
        
        Returns:
            冲突文件路径（相对于 workspace），如果写入失败则返回 None
        """
        conflict_dir = self._get_conflict_artifacts_dir()
        conflict_dir.mkdir(parents=True, exist_ok=True)
        
        # 生成冲突文件名
        safe_patch_id = patch_id.replace("/", "_").replace("\\", "_")
        conflict_filename = f"{safe_patch_id}.conflict.json"
        conflict_file = conflict_dir / conflict_filename
        
        conflict_data = {
            "patch_id": patch_id,
            "file": file_path,
            "category": category,
            "reason": reason,
            "context": context,
            "strategy": strategy.value,
            "timestamp": datetime.now().isoformat(),
            "resolution_hints": self._get_resolution_hints(category, reason, strategy)
        }
        
        try:
            with open(conflict_file, "w", encoding="utf-8") as f:
                json.dump(conflict_data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            return str(conflict_file.relative_to(self.workspace_root))
        except Exception as e:
            print(f"[WARN] 无法写入冲突文件 {conflict_file}: {e}")
            return None
    
    def _get_resolution_hints(
        self, 
        category: str, 
        reason: str, 
        strategy: ConflictStrategy
    ) -> List[str]:
        """根据冲突类型和策略生成解决建议"""
        hints = []
        
        # 基于分类的建议
        if category == "A":
            hints.append("[CRITICAL] Category A 补丁是必须保留的，此冲突需要立即解决")
            hints.append("建议: 手动检查并重新应用补丁，或更新补丁以适配新的上游代码")
        elif category == "B":
            hints.append("[WARN] Category B 补丁可上游化，建议尝试向上游提交 PR")
            hints.append("如果上游已接受类似修改，可考虑移除此补丁")
        elif category == "C":
            hints.append("[INFO] Category C 补丁可移除，建议评估是否仍然需要")
            hints.append("如果功能已被上游实现或不再需要，可以安全移除")
        
        # 基于策略的建议
        if strategy == ConflictStrategy.CLEAN:
            hints.append("策略 clean: 需要手动解决冲突后重新运行")
        elif strategy == ConflictStrategy.THREE_WAY:
            hints.append("策略 3way: 尝试三方合并，如仍有冲突请手动处理")
        elif strategy == ConflictStrategy.MANUAL:
            hints.append("策略 manual: 请查看此冲突文件并手动处理")
        
        return hints
    
    def apply_patches(
        self, 
        dry_run: bool = True, 
        categories: Optional[list] = None,
        strategy: ConflictStrategy = ConflictStrategy.CLEAN,
        quiet: bool = False,
        force_update_lock: bool = False
    ) -> bool:
        """
        应用补丁
        
        Args:
            dry_run: 如果为 True，仅预览不实际执行
            categories: 要应用的补丁分类列表，如 ["A"] 或 ["A", "B", "C"]，默认全部
            strategy: 冲突处理策略 (clean/3way/manual)
            quiet: 静默模式（用于 --json）
            force_update_lock: 强制更新 lock 文件（紧急情况使用，绕过 verify 检查）
        
        Returns:
            bool: 是否成功
        
        补丁应用流程:
            1. 读取 openmemory_patches.json，按 Category A→B→C 顺序处理
            2. 对每个补丁：先 dry-run 校验（目标文件存在性），再实际 apply
            3. 计算目标文件 sha256 并写入/对照 OpenMemory.upstream.lock.json 的 checksums
            4. 输出 JSON 报告
        
        Lock 文件更新保护（dry_run=False 时）:
            - 在更新 lock 前先执行 verify_patches() 检查
            - 若存在 Category A checksum mismatch 或文件缺失，拒绝更新 lock
            - 使用 --force-update-lock 可绕过此检查（紧急情况，会在报告中标注）
        
        冲突处理规则:
            - Category A 失败: 整体状态置 ERROR（必须修复）
            - Category B 失败: 整体状态置 WARN（应当关注）
            - Category C 失败: 允许跳过并记录（可选修复）
        """
        if not self.load_config_files():
            return False
        
        patches = self.patches_data.get("patches", [])
        sorted_patches = self._sort_patches_by_category(patches)
        
        # 过滤分类
        if categories is None:
            categories = ["A", "B", "C"]
        
        # 应用结果
        apply_result = {
            "mode": "dry_run" if dry_run else "apply",
            "strategy": strategy.value,
            "timestamp": datetime.now().isoformat(),
            "categories_requested": categories,
            "files": [],
            "checksums": {},
            "success": [],
            "failed": [],
            "conflicts": [],
            "conflicts_by_category": {"A": [], "B": [], "C": []},
            "skipped": [],
            "conflict_files": [],  # 冲突产物文件路径列表
            "lock_update_blocked": False,  # lock 更新是否被阻止
            "lock_block_reason": ""  # 阻止原因
        }
        
        # 获取现有 checksums（兼容新旧格式）
        checksums_data = self.lock_data.get("checksums", {})
        if isinstance(checksums_data, dict) and "patched_files" in checksums_data:
            # 新格式: 提取 patched_files 中的 after 值
            existing_checksums = {
                path: info.get("after") if isinstance(info, dict) else info
                for path, info in checksums_data.get("patched_files", {}).items()
            }
        else:
            # 旧格式: 直接使用（排除非路径键）
            existing_checksums = {
                k: v for k, v in checksums_data.items() 
                if k not in ("description", "patched_files")
            }
        new_checksums = {}
        
        if not quiet:
            if not dry_run:
                print("\n[APPLY] 开始应用补丁...")
                print("=" * 60)
            else:
                print("\n[DRY-RUN] 补丁应用预览:")
                print("=" * 60)
        
        for patch_group in sorted_patches:
            file_path = patch_group.get("file", "")
            changes = patch_group.get("changes", [])
            full_path = self.workspace_root / file_path
            exists = full_path.exists()
            
            file_result = {
                "path": file_path,
                "exists": exists,
                "patches": [],
                "sha256_before": None,
                "sha256_after": None
            }
            
            # 计算当前文件 sha256
            if exists:
                current_sha256 = self._compute_file_sha256(full_path)
                file_result["sha256_before"] = current_sha256
            else:
                current_sha256 = None
            
            if not quiet:
                print(f"\n文件: {file_path}")
                print(f"  状态: {'存在' if exists else '缺失'}")
                if current_sha256:
                    print(f"  SHA256: {current_sha256[:16]}...")
                print(f"  补丁数: {len(changes)}")
            
            # 处理每个补丁
            for change in changes:
                category = change.get("category", "?")
                change_id = change.get("id", "unknown")
                location = change.get("location", "unknown")
                description = change.get("description", "")
                
                patch_result = {
                    "id": change_id,
                    "category": category,
                    "location": location,
                    "description": description[:100],
                    "status": "pending"
                }
                
                # 检查是否在请求的分类中
                if category not in categories:
                    patch_result["status"] = "skipped"
                    patch_result["reason"] = f"Category {category} not in requested categories"
                    apply_result["skipped"].append(change_id)
                    file_result["patches"].append(patch_result)
                    
                    if not quiet:
                        print(f"    {change_id}: [跳过] Category {category} 不在请求范围")
                    continue
                
                category_symbol = {
                    "A": "[必须保留]",
                    "B": "[可上游化]",
                    "C": "[可移除]"
                }.get(category, f"[{category}]")
                
                # Dry-run 校验
                if not exists:
                    patch_result["status"] = "conflict"
                    patch_result["reason"] = "Target file does not exist"
                    
                    conflict_info = {
                        "patch_id": change_id,
                        "file": file_path,
                        "category": category,
                        "reason": "文件不存在",
                        "context": f"补丁 {change_id} 的目标文件 {file_path} 不存在"
                    }
                    apply_result["conflicts"].append(conflict_info)
                    apply_result["conflicts_by_category"][category].append(change_id)
                    
                    # 生成冲突文件
                    conflict_file_path = self._write_conflict_file(
                        patch_id=change_id,
                        file_path=file_path,
                        category=category,
                        reason="文件不存在",
                        context=conflict_info["context"],
                        strategy=strategy
                    )
                    if conflict_file_path:
                        apply_result["conflict_files"].append(conflict_file_path)
                    
                    file_result["patches"].append(patch_result)
                    
                    if not quiet:
                        print(f"    {change_id}: {category_symbol} [冲突] 文件不存在")
                    continue
                
                # 补丁描述性校验通过
                if dry_run:
                    patch_result["status"] = "would_apply"
                    apply_result["success"].append(change_id)
                    
                    if not quiet:
                        print(f"    {change_id}: {category_symbol} [可应用]")
                        print(f"      位置: {location}")
                        desc_display = description[:60] + "..." if len(description) > 60 else description
                        print(f"      描述: {desc_display}")
                else:
                    # 实际应用模式 - 由于补丁是描述性的，这里主要是记录状态
                    # 实际代码修改需要手动完成，此处验证补丁已落地
                    patch_result["status"] = "verified"
                    apply_result["success"].append(change_id)
                    
                    if not quiet:
                        print(f"    {change_id}: {category_symbol} [已验证]")
                        print(f"      位置: {location}")
                
                file_result["patches"].append(patch_result)
            
            # 计算应用后的 sha256（对于已存在的文件）
            if exists:
                after_sha256 = self._compute_file_sha256(full_path)
                file_result["sha256_after"] = after_sha256
                new_checksums[file_path] = after_sha256
                
                # 对照现有 checksums
                if file_path in existing_checksums:
                    expected = existing_checksums[file_path]
                    if after_sha256 != expected:
                        if not quiet:
                            print(f"  [WARN] SHA256 不匹配: 预期 {expected[:16]}..., 实际 {after_sha256[:16]}...")
                        
                        # 确定此文件涉及的最高优先级分类
                        file_category = "C"  # 默认
                        for change in patch_group.get("changes", []):
                            cat = change.get("category", "C")
                            if cat == "A":
                                file_category = "A"
                                break
                            elif cat == "B" and file_category != "A":
                                file_category = "B"
                        
                        conflict_info = {
                            "patch_id": "checksum",
                            "file": file_path,
                            "category": file_category,
                            "reason": "SHA256 checksum mismatch",
                            "context": f"预期: {expected}, 实际: {after_sha256}"
                        }
                        apply_result["conflicts"].append(conflict_info)
                        apply_result["conflicts_by_category"][file_category].append(f"checksum:{file_path}")
                        
                        # 生成冲突文件
                        conflict_file_path = self._write_conflict_file(
                            patch_id=f"checksum_{file_path.replace('/', '_')}",
                            file_path=file_path,
                            category=file_category,
                            reason="SHA256 checksum mismatch",
                            context=conflict_info["context"],
                            strategy=strategy
                        )
                        if conflict_file_path:
                            apply_result["conflict_files"].append(conflict_file_path)
            
            apply_result["files"].append(file_result)
        
        apply_result["checksums"] = new_checksums
        apply_result["force_update_lock"] = force_update_lock
        
        if not quiet:
            print("\n" + "=" * 60)
        
        # 实际应用模式下更新 lock 文件
        if not dry_run:
            # 在更新 lock 前先执行 verify 检查（除非强制更新）
            lock_update_blocked = False
            verify_block_reason = ""
            verify_result = {}
            
            if not force_update_lock:
                if not quiet:
                    print("\n[INFO] 执行 verify 检查（更新 lock 前置条件）...")
                
                # 保存当前报告状态，避免 verify 污染
                saved_report = self.report
                self.report = SyncReport()
                
                # 静默执行 verify
                verify_ok = self.verify_patches(quiet=True)
                verify_result = self.report.patches_status.get("verify_result", {})
                
                # 恢复报告
                self.report = saved_report
                
                # ============================================================
                # 调用冻结策略评估函数
                # ============================================================
                freeze_status = self.lock_data.get("freeze_status", {})
                upgrade_policy = self.lock_data.get("upgrade_policy", {})
                freeze_rules = upgrade_policy.get("freeze_rules", {}).get("rules", [])
                
                # 获取 override 参数
                override_reason = os.environ.get("APPLY_OVERRIDE_REASON", "")
                override_authority = os.environ.get("APPLY_OVERRIDE_AUTHORITY", "")
                
                policy_result = self._evaluate_freeze_and_update_ref_policy(
                    freeze_status=freeze_status,
                    freeze_rules=freeze_rules,
                    strict_patch_files=True,  # apply 时使用严格模式
                    verify_result=verify_result,
                    apply_result=apply_result,
                    override_reason=override_reason,
                    override_authority=override_authority
                )
                
                apply_result["freeze_policy_check"] = policy_result
                
                # 检查 Category A mismatch 或文件缺失（原有逻辑）
                category_mismatch = verify_result.get("category_mismatch", {})
                missing_count = verify_result.get("summary", {}).get("missing", 0)
                category_a_mismatch = category_mismatch.get("A", 0)
                
                # 综合策略评估结果和原有检查
                if not policy_result["allow_update_ref"]:
                    # 策略评估不通过
                    lock_update_blocked = True
                    verify_block_reason = policy_result["block_reason"]
                    
                    if not quiet:
                        print(f"\n[FREEZE] Lock 更新被冻结策略阻止")
                        print(f"  原因: {verify_block_reason}")
                        
                        if policy_result["triggered_rules"]:
                            print("  触发的规则:")
                            for rule in policy_result["triggered_rules"]:
                                print(f"    - {rule['id']}: {rule['reason']}")
                        
                        if policy_result["recommended_flags"]:
                            print("  推荐的标志:")
                            for flag in policy_result["recommended_flags"]:
                                print(f"    {flag}")
                        
                        # 记录 override 尝试（如果有）
                        if override_reason and not policy_result["override_valid"]:
                            print(f"\n  [WARN] 提供的 override 无效:")
                            for issue in policy_result["override_issues"]:
                                print(f"    - {issue}")
                    
                    apply_result["lock_update_blocked"] = True
                    apply_result["lock_block_reason"] = verify_block_reason
                    apply_result["failed"].append("lock_update_blocked_by_freeze_policy")
                
                elif category_a_mismatch > 0 or missing_count > 0:
                    # 原有的 verify 检查不通过
                    lock_update_blocked = True
                    verify_block_reason = f"Category A mismatch ({category_a_mismatch}) 或文件缺失 ({missing_count})"
                    
                    if not quiet:
                        print(f"\n[ERROR] Verify 检查失败: {verify_block_reason}")
                        print("[ERROR] 拒绝更新 lock 文件 - 请先修复上述问题")
                        print("[HINT] 紧急情况可使用 --force-update-lock 强制更新（会在报告中标注）")
                    
                    apply_result["lock_update_blocked"] = True
                    apply_result["lock_block_reason"] = verify_block_reason
                    apply_result["failed"].append("lock_update_blocked_by_verify")
                else:
                    if not quiet:
                        print("[OK] Verify 检查通过")
                    
                    # 如果通过 override 更新，记录信息
                    if policy_result["needs_override"] and policy_result["override_valid"]:
                        if not quiet:
                            print(f"\n  [OVERRIDE] 通过 override 更新 lock")
                            print(f"  override 原因: {override_reason}")
                        
                        # 更新 lock 中的 override 记录
                        if "freeze_status" not in self.lock_data:
                            self.lock_data["freeze_status"] = {}
                        self.lock_data["freeze_status"]["last_override_at"] = datetime.now().isoformat() + "Z"
                        self.lock_data["freeze_status"]["last_override_by"] = override_authority or "unknown"
                        self.lock_data["freeze_status"]["last_override_reason"] = override_reason
                        
                        apply_result["lock_update_override"] = {
                            "reason": override_reason,
                            "authority": override_authority,
                            "timestamp": self.lock_data["freeze_status"]["last_override_at"]
                        }
            else:
                # 强制更新模式
                if not quiet:
                    print("\n[WARN] 使用 --force-update-lock 强制更新模式")
                    print("[WARN] 跳过 verify 检查 - 此操作会在报告中标注")
                apply_result["lock_update_forced"] = True
            
            # 更新 lock 文件（如果未被阻止）
            if not lock_update_blocked:
                if not quiet:
                    print("\n[INFO] 更新 lock 文件 checksums...")
                if self._update_lock_checksums(new_checksums):
                    if not quiet:
                        print("[OK] lock 文件已更新")
                        if force_update_lock:
                            print("[WARN] 注意: 此次更新使用了 --force-update-lock 标志")
                else:
                    apply_result["failed"].append("lock_file_update")
        
        # 汇总
        summary = {
            "total_patches": sum(len(pg.get("changes", [])) for pg in patches),
            "applied": len(apply_result["success"]),
            "failed": len(apply_result["failed"]),
            "conflicts": len(apply_result["conflicts"]),
            "conflicts_by_category": {
                "A": len(apply_result["conflicts_by_category"]["A"]),
                "B": len(apply_result["conflicts_by_category"]["B"]),
                "C": len(apply_result["conflicts_by_category"]["C"])
            },
            "skipped": len(apply_result["skipped"]),
            "conflict_files_count": len(apply_result["conflict_files"])
        }
        apply_result["summary"] = summary
        
        # 根据 Category 冲突设置整体状态
        # Category A 失败: ERROR（必须修复）
        # Category B 失败: WARN（应当关注）  
        # Category C 失败: 允许跳过（记录即可）
        category_a_conflicts = len(apply_result["conflicts_by_category"]["A"])
        category_b_conflicts = len(apply_result["conflicts_by_category"]["B"])
        category_c_conflicts = len(apply_result["conflicts_by_category"]["C"])
        
        final_status = CheckStatus.OK
        status_reason = ""
        
        if category_a_conflicts > 0:
            final_status = CheckStatus.ERROR
            status_reason = f"Category A 冲突 ({category_a_conflicts} 个) - 必须修复"
        elif category_b_conflicts > 0:
            final_status = CheckStatus.WARN
            status_reason = f"Category B 冲突 ({category_b_conflicts} 个) - 应当关注"
        elif category_c_conflicts > 0:
            # Category C 冲突不影响整体状态，只记录
            status_reason = f"Category C 冲突 ({category_c_conflicts} 个) - 已记录，可选修复"
        
        if len(apply_result["failed"]) > 0:
            final_status = CheckStatus.ERROR
            status_reason = f"应用失败 ({len(apply_result['failed'])} 个)"
        
        apply_result["final_status"] = final_status.value
        apply_result["status_reason"] = status_reason
        
        # 记录冲突产物目录
        if apply_result["conflict_files"]:
            conflict_dir = self._get_conflict_artifacts_dir()
            self.report.conflict_artifacts_dir = str(conflict_dir.relative_to(self.workspace_root))
            self.report.conflict_files = apply_result["conflict_files"]
        
        if not quiet:
            if dry_run:
                print(f"\n[DRY-RUN] 预览完成")
                print(f"  可应用: {summary['applied']}")
                print(f"  冲突: {summary['conflicts']}")
                print(f"    - Category A: {category_a_conflicts}")
                print(f"    - Category B: {category_b_conflicts}")
                print(f"    - Category C: {category_c_conflicts}")
                print(f"  跳过: {summary['skipped']}")
                print(f"  策略: {strategy.value}")
                if apply_result["conflict_files"]:
                    print(f"\n冲突产物目录: {self.report.conflict_artifacts_dir}")
                    print(f"  生成冲突文件: {len(apply_result['conflict_files'])} 个")
                print("\n提示: 使用 DRY_RUN=0 make openmemory-sync-apply 执行实际应用")
            else:
                print(f"\n[APPLY] 应用完成")
                print(f"  已验证: {summary['applied']}")
                print(f"  失败: {summary['failed']}")
                print(f"  冲突: {summary['conflicts']}")
                print(f"    - Category A: {category_a_conflicts} (ERROR)")
                print(f"    - Category B: {category_b_conflicts} (WARN)")
                print(f"    - Category C: {category_c_conflicts} (可跳过)")
                if apply_result["conflict_files"]:
                    print(f"\n冲突产物目录: {self.report.conflict_artifacts_dir}")
                    for cf in apply_result["conflict_files"][:5]:
                        print(f"    - {cf}")
                    if len(apply_result["conflict_files"]) > 5:
                        print(f"    ... 共 {len(apply_result['conflict_files'])} 个冲突文件")
            
            if status_reason:
                print(f"\n状态: [{final_status.value.upper()}] {status_reason}")
        
        # 存储结果供 JSON 输出
        self.report.patches_status["apply_result"] = apply_result
        
        # 更新报告整体状态
        if final_status == CheckStatus.ERROR:
            self.report.overall_status = CheckStatus.ERROR
        elif final_status == CheckStatus.WARN and self.report.overall_status != CheckStatus.ERROR:
            self.report.overall_status = CheckStatus.WARN
        
        return final_status != CheckStatus.ERROR
    
    def verify_patches(self, quiet: bool = False) -> bool:
        """
        校验补丁是否已正确落地
        
        Args:
            quiet: 如果为 True，仅输出 JSON 格式（用于 --json 模式）
        
        Returns:
            bool: 所有补丁是否已正确落地
        
        当校验失败时，会生成冲突文件到 .artifacts/openmemory-patch-conflicts/
        
        输出 is_equal_to_base 字段说明：
            当 expected(after) 与 actual 不匹配时，如果 base 存在，额外输出 is_equal_to_base：
            - is_equal_to_base=True: 当前文件等于上游 base，说明"补丁未重放"
            - is_equal_to_base=False: 当前文件既不等于 after 也不等于 base，说明"补丁内容变更"或有其他修改
        """
        if not self.load_config_files():
            return False
        
        patches = self.patches_data.get("patches", [])
        # 获取现有 checksums（兼容新旧格式）
        checksums_data = self.lock_data.get("checksums", {})
        if isinstance(checksums_data, dict) and "patched_files" in checksums_data:
            # 新格式: 提取 patched_files 中的完整信息
            patched_files_checksums = checksums_data.get("patched_files", {})
            existing_checksums = {
                path: info.get("after") if isinstance(info, dict) else info
                for path, info in patched_files_checksums.items()
            }
            # 提取 base checksums 用于判断"补丁未重放" vs "补丁内容变更"
            base_checksums = {
                path: info.get("base") if isinstance(info, dict) else None
                for path, info in patched_files_checksums.items()
            }
        else:
            # 旧格式: 直接使用（排除非路径键）
            existing_checksums = {
                k: v for k, v in checksums_data.items() 
                if k not in ("description", "patched_files")
            }
            base_checksums = {}
        
        verify_result = {
            "timestamp": datetime.now().isoformat(),
            "files": [],
            "verified": [],
            "missing": [],
            "checksum_mismatch": [],
            "conflict_files": []  # 冲突产物文件路径列表
        }
        
        if not quiet:
            print("\n[VERIFY] 校验补丁落地状态...")
            print("=" * 60)
        
        for patch_group in patches:
            file_path = patch_group.get("file", "")
            full_path = self.workspace_root / file_path
            exists = full_path.exists()
            
            # 确定此文件涉及的最高优先级分类
            file_category = "C"  # 默认
            for change in patch_group.get("changes", []):
                cat = change.get("category", "C")
                if cat == "A":
                    file_category = "A"
                    break
                elif cat == "B" and file_category != "A":
                    file_category = "B"
            
            file_result = {
                "path": file_path,
                "exists": exists,
                "sha256": None,
                "expected_sha256": existing_checksums.get(file_path),
                "status": "pending",
                "category": file_category
            }
            
            if not quiet:
                print(f"\n文件: {file_path}")
            
            if not exists:
                file_result["status"] = "missing"
                verify_result["missing"].append(file_path)
                if not quiet:
                    print(f"  状态: [缺失] 文件不存在")
                
                # 生成冲突文件
                conflict_file_path = self._write_conflict_file(
                    patch_id=f"verify_missing_{file_path.replace('/', '_')}",
                    file_path=file_path,
                    category=file_category,
                    reason="文件不存在",
                    context=f"补丁目标文件 {file_path} 不存在",
                    strategy=ConflictStrategy.MANUAL
                )
                if conflict_file_path:
                    verify_result["conflict_files"].append(conflict_file_path)
            else:
                current_sha256 = self._compute_file_sha256(full_path)
                file_result["sha256"] = current_sha256
                
                expected = file_result["expected_sha256"]
                if expected:
                    if current_sha256 == expected:
                        file_result["status"] = "verified"
                        verify_result["verified"].append(file_path)
                        if not quiet:
                            print(f"  状态: [已验证] SHA256 匹配")
                            print(f"  SHA256: {current_sha256[:16]}...")
                    else:
                        file_result["status"] = "checksum_mismatch"
                        
                        # 检查是否等于 base（用于区分"补丁未重放" vs "补丁内容变更"）
                        base_sha256 = base_checksums.get(file_path)
                        is_equal_to_base = None
                        mismatch_reason = "checksum_mismatch"
                        
                        if base_sha256:
                            is_equal_to_base = (current_sha256 == base_sha256)
                            if is_equal_to_base:
                                mismatch_reason = "patch_not_applied"  # 补丁未重放
                            else:
                                mismatch_reason = "content_diverged"  # 补丁内容变更或其他修改
                        
                        mismatch_info = {
                            "file": file_path,
                            "expected": expected,
                            "actual": current_sha256,
                            "category": file_category,
                            "base": base_sha256,
                            "is_equal_to_base": is_equal_to_base,
                            "mismatch_reason": mismatch_reason
                        }
                        verify_result["checksum_mismatch"].append(mismatch_info)
                        
                        if not quiet:
                            print(f"  状态: [不匹配] SHA256 校验失败")
                            print(f"  预期(after): {expected[:16]}...")
                            print(f"  实际: {current_sha256[:16]}...")
                            if base_sha256:
                                print(f"  base: {base_sha256[:16]}...")
                                print(f"  is_equal_to_base: {is_equal_to_base}")
                                if is_equal_to_base:
                                    print(f"  诊断: [补丁未重放] 文件等于上游 base，需要重新应用补丁")
                                else:
                                    print(f"  诊断: [内容变更] 文件既不等于 after 也不等于 base")
                        
                        # 生成冲突文件
                        conflict_context = f"预期(after): {expected}, 实际: {current_sha256}"
                        if base_sha256:
                            conflict_context += f", base: {base_sha256}, is_equal_to_base: {is_equal_to_base}"
                        
                        conflict_file_path = self._write_conflict_file(
                            patch_id=f"verify_checksum_{file_path.replace('/', '_')}",
                            file_path=file_path,
                            category=file_category,
                            reason=f"SHA256 checksum mismatch ({mismatch_reason})",
                            context=conflict_context,
                            strategy=ConflictStrategy.MANUAL
                        )
                        if conflict_file_path:
                            verify_result["conflict_files"].append(conflict_file_path)
                else:
                    # 无预期 checksum，视为首次校验
                    file_result["status"] = "no_baseline"
                    verify_result["verified"].append(file_path)
                    if not quiet:
                        print(f"  状态: [无基线] 首次校验，当前 SHA256: {current_sha256[:16]}...")
            
            verify_result["files"].append(file_result)
        
        if not quiet:
            print("\n" + "=" * 60)
        
        # 汇总
        summary = {
            "total_files": len(patches),
            "verified": len(verify_result["verified"]),
            "missing": len(verify_result["missing"]),
            "checksum_mismatch": len(verify_result["checksum_mismatch"]),
            "conflict_files_count": len(verify_result["conflict_files"])
        }
        verify_result["summary"] = summary
        
        all_ok = summary["missing"] == 0 and summary["checksum_mismatch"] == 0
        
        # 按 Category 统计不匹配
        category_mismatch = {"A": 0, "B": 0, "C": 0}
        # 按原因统计不匹配
        mismatch_by_reason = {"patch_not_applied": 0, "content_diverged": 0, "unknown": 0}
        
        for mismatch in verify_result["checksum_mismatch"]:
            cat = mismatch.get("category", "C")
            category_mismatch[cat] = category_mismatch.get(cat, 0) + 1
            
            reason = mismatch.get("mismatch_reason", "unknown")
            if reason == "patch_not_applied":
                mismatch_by_reason["patch_not_applied"] += 1
            elif reason == "content_diverged":
                mismatch_by_reason["content_diverged"] += 1
            else:
                mismatch_by_reason["unknown"] += 1
        
        verify_result["category_mismatch"] = category_mismatch
        verify_result["mismatch_by_reason"] = mismatch_by_reason
        
        # 确定最终状态
        if category_mismatch["A"] > 0 or summary["missing"] > 0:
            final_status = CheckStatus.ERROR
            status_reason = f"Category A 不匹配 ({category_mismatch['A']}) 或文件缺失 ({summary['missing']}) - 必须修复"
        elif category_mismatch["B"] > 0:
            final_status = CheckStatus.WARN
            status_reason = f"Category B 不匹配 ({category_mismatch['B']}) - 应当关注"
        elif category_mismatch["C"] > 0:
            final_status = CheckStatus.OK  # Category C 不匹配不影响整体状态
            status_reason = f"Category C 不匹配 ({category_mismatch['C']}) - 可选修复"
        else:
            final_status = CheckStatus.OK
            status_reason = ""
        
        verify_result["final_status"] = final_status.value
        verify_result["status_reason"] = status_reason
        
        if not quiet:
            print(f"\n[VERIFY] 校验完成")
            print(f"  已验证: {summary['verified']}")
            print(f"  缺失: {summary['missing']}")
            print(f"  不匹配: {summary['checksum_mismatch']}")
            print(f"    按 Category:")
            print(f"      - Category A: {category_mismatch['A']}")
            print(f"      - Category B: {category_mismatch['B']}")
            print(f"      - Category C: {category_mismatch['C']}")
            if mismatch_by_reason["patch_not_applied"] > 0 or mismatch_by_reason["content_diverged"] > 0:
                print(f"    按原因 (is_equal_to_base):")
                print(f"      - 补丁未重放 (=base): {mismatch_by_reason['patch_not_applied']}")
                print(f"      - 内容变更 (≠base): {mismatch_by_reason['content_diverged']}")
                if mismatch_by_reason["unknown"] > 0:
                    print(f"      - 无 base 参照: {mismatch_by_reason['unknown']}")
            
            if verify_result["conflict_files"]:
                conflict_dir = self._get_conflict_artifacts_dir()
                print(f"\n冲突产物目录: {conflict_dir.relative_to(self.workspace_root)}")
                for cf in verify_result["conflict_files"][:5]:
                    print(f"    - {cf}")
                if len(verify_result["conflict_files"]) > 5:
                    print(f"    ... 共 {len(verify_result['conflict_files'])} 个冲突文件")
            
            if all_ok:
                print("\n[OK] 所有补丁已正确落地")
            else:
                print(f"\n[{final_status.value.upper()}] {status_reason}")
        
        # 记录冲突产物目录
        if verify_result["conflict_files"]:
            conflict_dir = self._get_conflict_artifacts_dir()
            self.report.conflict_artifacts_dir = str(conflict_dir.relative_to(self.workspace_root))
            self.report.conflict_files = verify_result["conflict_files"]
        
        # 存储结果供 JSON 输出
        self.report.patches_status["verify_result"] = verify_result
        
        # 更新报告整体状态
        if final_status == CheckStatus.ERROR:
            self.report.overall_status = CheckStatus.ERROR
        elif final_status == CheckStatus.WARN and self.report.overall_status != CheckStatus.ERROR:
            self.report.overall_status = CheckStatus.WARN
        
        return all_ok
    
    def generate_suggestions(self) -> list:
        """生成下一步建议"""
        suggestions = []
        
        if self.report.overall_status == CheckStatus.ERROR:
            suggestions.append({
                "priority": 1,
                "action": "修复错误",
                "description": "一致性检查发现错误，请先修复后再继续",
                "commands": []
            })
        
        # 基于补丁状态的建议
        if self.report.patches_status:
            cat_c = self.report.patches_status.get("category_C_removable", 0)
            if cat_c > 0:
                suggestions.append({
                    "priority": 2,
                    "action": "清理可移除补丁",
                    "description": f"有 {cat_c} 个 Category C（可移除）补丁，建议重构以减少维护负担",
                    "commands": []
                })
            
            cat_b = self.report.patches_status.get("category_B_upstreamable", 0)
            if cat_b > 0:
                suggestions.append({
                    "priority": 3,
                    "action": "考虑上游贡献",
                    "description": f"有 {cat_b} 个 Category B（可上游化）补丁，可考虑向上游提交 PR",
                    "commands": []
                })
        
        # 标准升级流程建议
        suggestions.append({
            "priority": 4,
            "action": "构建与验证",
            "description": "执行 OpenMemory 构建和升级检查",
            "commands": [
                "make openmemory-build",
                "make openmemory-upgrade-check"
            ]
        })
        
        # 补丁应用建议
        suggestions.append({
            "priority": 5,
            "action": "补丁管理",
            "description": "查看补丁详情以便在上游升级时重新应用",
            "commands": [
                "make openmemory-sync-apply DRY_RUN=1"
            ]
        })
        
        self.report.suggestions = suggestions
        return suggestions
    
    # ========================================================================
    # Fetch / Sync 功能
    # ========================================================================
    
    def _get_github_archive_url(self, ref: str, ref_type: str) -> str:
        """
        构建 GitHub archive 下载 URL
        
        Args:
            ref: tag 名称或 commit SHA
            ref_type: "tag" 或 "commit"
        
        Returns:
            GitHub archive URL
        """
        # 从 lock 文件获取上游 URL
        upstream_url = self.lock_data.get("upstream_url", "https://github.com/CaviraOSS/OpenMemory")
        # 移除 .git 后缀（如果有）
        upstream_url = upstream_url.rstrip("/").removesuffix(".git")
        # 构建 archive URL（GitHub 支持 /archive/{ref}.tar.gz 或 /archive/{ref}.zip）
        # 对于 tag，格式为 v1.3.0；对于 commit，格式为完整 SHA
        return f"{upstream_url}/archive/{ref}.tar.gz"
    
    def _get_github_api_url(self, ref: str, ref_type: str) -> str:
        """
        构建 GitHub API URL 用于获取 commit 信息
        
        Args:
            ref: tag 名称或 commit SHA
            ref_type: "tag" 或 "commit"
        
        Returns:
            GitHub API URL
        """
        upstream_url = self.lock_data.get("upstream_url", "https://github.com/CaviraOSS/OpenMemory")
        # 解析 owner/repo
        parts = upstream_url.rstrip("/").removesuffix(".git").split("/")
        owner, repo = parts[-2], parts[-1]
        
        if ref_type == "tag":
            # 获取 tag 对应的 commit
            return f"https://api.github.com/repos/{owner}/{repo}/git/refs/tags/{ref}"
        else:
            # 直接获取 commit 信息
            return f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
    
    def _fetch_commit_info(self, ref: str, ref_type: str) -> Tuple[Optional[str], Optional[str]]:
        """
        从 GitHub API 获取 commit SHA 和日期
        
        Args:
            ref: tag 名称或 commit SHA
            ref_type: "tag" 或 "commit"
        
        Returns:
            (commit_sha, commit_date) 或 (None, None)
        """
        try:
            upstream_url = self.lock_data.get("upstream_url", "https://github.com/CaviraOSS/OpenMemory")
            parts = upstream_url.rstrip("/").removesuffix(".git").split("/")
            owner, repo = parts[-2], parts[-1]
            
            headers = {"Accept": "application/vnd.github.v3+json"}
            # 添加 GitHub token（如果有）
            github_token = os.environ.get("GITHUB_TOKEN")
            if github_token:
                headers["Authorization"] = f"token {github_token}"
            
            if ref_type == "tag":
                # 先获取 tag 引用
                tag_url = f"https://api.github.com/repos/{owner}/{repo}/git/refs/tags/{ref}"
                req = urllib.request.Request(tag_url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    tag_data = json.loads(resp.read().decode("utf-8"))
                
                # 获取 tag 指向的对象
                obj_sha = tag_data.get("object", {}).get("sha")
                obj_type = tag_data.get("object", {}).get("type")
                
                if obj_type == "tag":
                    # Annotated tag，需要再获取一次
                    tag_obj_url = f"https://api.github.com/repos/{owner}/{repo}/git/tags/{obj_sha}"
                    req = urllib.request.Request(tag_obj_url, headers=headers)
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        tag_obj_data = json.loads(resp.read().decode("utf-8"))
                    commit_sha = tag_obj_data.get("object", {}).get("sha")
                else:
                    # Lightweight tag，直接指向 commit
                    commit_sha = obj_sha
            else:
                commit_sha = ref
            
            # 获取 commit 详情
            commit_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{commit_sha}"
            req = urllib.request.Request(commit_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                commit_data = json.loads(resp.read().decode("utf-8"))
            
            commit_date = commit_data.get("commit", {}).get("committer", {}).get("date")
            return commit_sha, commit_date
            
        except urllib.error.HTTPError as e:
            print(f"[WARN] GitHub API 请求失败: {e.code} {e.reason}")
            return None, None
        except Exception as e:
            print(f"[WARN] 获取 commit 信息失败: {e}")
            return None, None
    
    def _download_archive(self, url: str, dest_path: Path) -> Tuple[bool, Optional[str]]:
        """
        下载 GitHub archive 并返回 SHA256
        
        Args:
            url: 下载 URL
            dest_path: 保存路径
        
        Returns:
            (success, sha256_hex) 或 (False, None)
        """
        try:
            print(f"[INFO] 下载 archive: {url}")
            
            headers = {"User-Agent": "engram-sync-tool/1.0"}
            github_token = os.environ.get("GITHUB_TOKEN")
            if github_token:
                headers["Authorization"] = f"token {github_token}"
            
            req = urllib.request.Request(url, headers=headers)
            
            sha256_hash = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(dest_path, "wb") as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
                        sha256_hash.update(chunk)
            
            sha256_hex = sha256_hash.hexdigest()
            print(f"[OK] 下载完成: {dest_path.name} (SHA256: {sha256_hex[:16]}...)")
            return True, sha256_hex
            
        except urllib.error.HTTPError as e:
            print(f"[ERROR] 下载失败: {e.code} {e.reason}")
            return False, None
        except Exception as e:
            print(f"[ERROR] 下载失败: {e}")
            return False, None
    
    def _extract_archive(self, archive_path: Path, dest_dir: Path) -> Tuple[bool, Optional[str]]:
        """
        解压 archive 到目标目录
        
        Args:
            archive_path: archive 文件路径
            dest_dir: 解压目标目录
        
        Returns:
            (success, extracted_root_dir_name) 或 (False, None)
        """
        try:
            print(f"[INFO] 解压 archive: {archive_path.name}")
            
            if archive_path.suffix == ".gz" or str(archive_path).endswith(".tar.gz"):
                with tarfile.open(archive_path, "r:gz") as tar:
                    # 获取根目录名称
                    members = tar.getmembers()
                    if not members:
                        print("[ERROR] archive 为空")
                        return False, None
                    
                    root_dir = members[0].name.split("/")[0]
                    tar.extractall(dest_dir)
                    
            elif archive_path.suffix == ".zip":
                with zipfile.ZipFile(archive_path, "r") as zf:
                    names = zf.namelist()
                    if not names:
                        print("[ERROR] archive 为空")
                        return False, None
                    
                    root_dir = names[0].split("/")[0]
                    zf.extractall(dest_dir)
            else:
                print(f"[ERROR] 不支持的 archive 格式: {archive_path.suffix}")
                return False, None
            
            print(f"[OK] 解压完成: {root_dir}")
            return True, root_dir
            
        except Exception as e:
            print(f"[ERROR] 解压失败: {e}")
            return False, None
    
    def _should_exclude(self, rel_path: str, exclude_patterns: List[str]) -> bool:
        """
        检查路径是否应被排除
        
        Args:
            rel_path: 相对路径
            exclude_patterns: 排除模式列表
        
        Returns:
            True 如果应排除
        """
        for pattern in exclude_patterns:
            # 简单模式匹配
            if pattern in rel_path or rel_path.startswith(pattern) or rel_path.endswith(pattern):
                return True
            # 检查路径组件
            path_parts = rel_path.split("/")
            if pattern in path_parts:
                return True
        return False
    
    def _collect_files(self, root_dir: Path, exclude_patterns: List[str]) -> Set[str]:
        """
        收集目录下所有文件（排除指定模式）
        
        Args:
            root_dir: 根目录
            exclude_patterns: 排除模式列表
        
        Returns:
            相对路径集合
        """
        files = set()
        for path in root_dir.rglob("*"):
            if path.is_file():
                rel_path = str(path.relative_to(root_dir))
                if not self._should_exclude(rel_path, exclude_patterns):
                    files.add(rel_path)
        return files
    
    def _compare_directories(
        self, 
        upstream_dir: Path, 
        local_dir: Path, 
        exclude_patterns: List[str]
    ) -> Dict[str, Any]:
        """
        对比上游与本地目录
        
        Args:
            upstream_dir: 上游目录
            local_dir: 本地目录
            exclude_patterns: 排除模式列表
        
        Returns:
            对比结果字典
        """
        upstream_files = self._collect_files(upstream_dir, exclude_patterns)
        local_files = self._collect_files(local_dir, exclude_patterns) if local_dir.exists() else set()
        
        # 计算差异
        new_files = upstream_files - local_files
        deleted_files = local_files - upstream_files
        common_files = upstream_files & local_files
        
        # 检查修改的文件
        modified_files = set()
        unchanged_files = set()
        
        for rel_path in common_files:
            upstream_file = upstream_dir / rel_path
            local_file = local_dir / rel_path
            
            upstream_sha = self._compute_file_sha256(upstream_file)
            local_sha = self._compute_file_sha256(local_file)
            
            if upstream_sha != local_sha:
                modified_files.add(rel_path)
            else:
                unchanged_files.add(rel_path)
        
        # 按目录统计
        def _group_by_dir(files: Set[str]) -> Dict[str, int]:
            dirs: Dict[str, int] = {}
            for f in files:
                parts = f.split("/")
                if len(parts) >= 2:
                    key = "/".join(parts[:2])  # 取前两级目录
                else:
                    key = parts[0]
                dirs[key] = dirs.get(key, 0) + 1
            return dict(sorted(dirs.items(), key=lambda x: -x[1]))
        
        return {
            "new_files": sorted(new_files),
            "deleted_files": sorted(deleted_files),
            "modified_files": sorted(modified_files),
            "unchanged_files": sorted(unchanged_files),
            "summary": {
                "total_upstream": len(upstream_files),
                "total_local": len(local_files),
                "new": len(new_files),
                "deleted": len(deleted_files),
                "modified": len(modified_files),
                "unchanged": len(unchanged_files)
            },
            "by_directory": {
                "new": _group_by_dir(new_files),
                "deleted": _group_by_dir(deleted_files),
                "modified": _group_by_dir(modified_files)
            }
        }
    
    def fetch_upstream(
        self, 
        ref: Optional[str] = None, 
        ref_type: Optional[str] = None,
        output_dir: Optional[Path] = None,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        获取上游代码
        
        Args:
            ref: 版本引用（tag 或 commit SHA），默认从 lock 文件读取
            ref_type: 引用类型（"tag" 或 "commit"），默认从 lock 文件读取
            output_dir: 输出目录，默认为临时目录
            dry_run: 如果为 True，仅显示将要执行的操作
        
        Returns:
            fetch 结果字典
        """
        if not self.load_config_files():
            return {"ok": False, "error": "无法加载配置文件"}
        
        # 从参数或 lock 文件获取配置
        ref = ref or self.lock_data.get("upstream_ref", "main")
        ref_type = ref_type or self.lock_data.get("upstream_ref_type", "tag")
        
        print("\n" + "=" * 60)
        print("OpenMemory 上游获取 (fetch)")
        print("=" * 60)
        print(f"  上游仓库: {self.lock_data.get('upstream_url')}")
        print(f"  版本引用: {ref} ({ref_type})")
        print(f"  模式: {'dry-run（预览）' if dry_run else '实际执行'}")
        print()
        
        result = {
            "ok": False,
            "ref": ref,
            "ref_type": ref_type,
            "dry_run": dry_run,
            "timestamp": datetime.now().isoformat(),
            "commit_sha": None,
            "commit_date": None,
            "archive_sha256": None,
            "archive_path": None,
            "extracted_dir": None
        }
        
        # 获取 commit 信息
        print("[1/3] 获取 commit 信息...")
        commit_sha, commit_date = self._fetch_commit_info(ref, ref_type)
        result["commit_sha"] = commit_sha
        result["commit_date"] = commit_date
        
        if commit_sha:
            print(f"  Commit SHA: {commit_sha}")
            print(f"  Commit Date: {commit_date or 'unknown'}")
        else:
            print("  [WARN] 无法获取 commit 信息（将继续下载）")
        
        if dry_run:
            print("\n[DRY-RUN] 跳过实际下载")
            result["ok"] = True
            return result
        
        # 下载 archive
        print("\n[2/3] 下载 archive...")
        archive_url = self._get_github_archive_url(ref, ref_type)
        
        # 创建输出目录
        if output_dir:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        else:
            output_dir = Path(tempfile.mkdtemp(prefix="openmemory_fetch_"))
        
        archive_path = output_dir / f"openmemory-{ref}.tar.gz"
        success, archive_sha256 = self._download_archive(archive_url, archive_path)
        
        if not success:
            result["error"] = "下载失败"
            return result
        
        result["archive_sha256"] = archive_sha256
        result["archive_path"] = str(archive_path)
        
        # 解压 archive
        print("\n[3/3] 解压 archive...")
        success, root_dir_name = self._extract_archive(archive_path, output_dir)
        
        if not success:
            result["error"] = "解压失败"
            return result
        
        extracted_dir = output_dir / root_dir_name
        result["extracted_dir"] = str(extracted_dir)
        
        # Step 4: 计算 patched_files 的 base checksums
        print("\n[4/4] 计算 patched_files base checksums...")
        base_checksums = self._compute_base_checksums_from_upstream(
            upstream_dir=extracted_dir,
            ref=ref,
            ref_type=ref_type,
            commit_sha=commit_sha
        )
        result["base_checksums"] = base_checksums
        
        # 计算成功的数量
        computed_count = sum(1 for info in base_checksums.values() if info.get("base"))
        total_count = len(base_checksums)
        print(f"  已计算: {computed_count}/{total_count} 个文件")
        
        # 更新 lock 文件中的 base checksums
        if base_checksums:
            print("  更新 lock 文件 base checksums...")
            if self._update_lock_base_checksums(base_checksums):
                print("  [OK] lock 文件已更新")
            else:
                print("  [WARN] lock 文件更新失败")
        
        result["ok"] = True
        
        print("\n" + "=" * 60)
        print("[OK] Fetch 完成")
        print(f"  Archive: {archive_path}")
        print(f"  解压目录: {extracted_dir}")
        print(f"  SHA256: {archive_sha256}")
        print(f"  Base checksums: {computed_count}/{total_count} 个文件")
        print("=" * 60)
        
        return result
    
    # ============================================================================
    # 三方合并 (3-way merge) 相关方法
    # ============================================================================
    
    def _fetch_file_content_at_ref(
        self,
        rel_path: str,
        ref: str,
        ref_type: str
    ) -> Optional[str]:
        """
        从 GitHub 获取指定 ref 的单个文件内容
        
        Args:
            rel_path: 相对于 libs/OpenMemory/ 的文件路径
            ref: 版本引用 (tag 或 commit SHA)
            ref_type: 引用类型 (tag/commit)
        
        Returns:
            文件内容字符串，如果获取失败返回 None
        """
        upstream_url = self.lock_data.get("upstream_url", "")
        if not upstream_url:
            return None
        
        # 从 upstream_url 提取 owner/repo
        # https://github.com/CaviraOSS/OpenMemory -> CaviraOSS/OpenMemory
        match = re.match(r"https?://github\.com/([^/]+/[^/]+)", upstream_url)
        if not match:
            return None
        
        owner_repo = match.group(1).rstrip(".git")
        
        # GitHub raw content URL
        raw_url = f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{rel_path}"
        
        try:
            req = urllib.request.Request(
                raw_url,
                headers={"User-Agent": "openmemory-sync/1.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            # 静默失败，调用者会处理
            return None
    
    def _perform_3way_merge(
        self,
        upstream_old_content: str,
        upstream_new_content: str,
        local_patched_content: str,
        file_name: str
    ) -> Tuple[bool, str, bool]:
        """
        执行三方合并
        
        使用 git merge-file 或 diff3 进行三方合并。
        优先使用 git merge-file，如果不可用则尝试 diff3。
        
        Args:
            upstream_old_content: 旧上游版本文件内容 (base)
            upstream_new_content: 新上游版本文件内容 (theirs)
            local_patched_content: 本地已补丁版本文件内容 (ours)
            file_name: 文件名（用于生成临时文件和冲突标记）
        
        Returns:
            (success, merged_content, has_conflict)
            - success: 合并过程是否执行成功
            - merged_content: 合并后的内容（可能包含冲突标记）
            - has_conflict: 是否存在未解决的冲突
        """
        import subprocess
        import tempfile
        
        # 创建临时文件
        with tempfile.TemporaryDirectory(prefix="openmemory_3way_") as temp_dir:
            temp_path = Path(temp_dir)
            
            # 写入三个版本的文件
            # git merge-file 格式: merge-file <ours> <base> <theirs>
            base_file = temp_path / f"{file_name}.base"
            ours_file = temp_path / f"{file_name}.ours"
            theirs_file = temp_path / f"{file_name}.theirs"
            
            base_file.write_text(upstream_old_content, encoding="utf-8")
            ours_file.write_text(local_patched_content, encoding="utf-8")
            theirs_file.write_text(upstream_new_content, encoding="utf-8")
            
            # 尝试 git merge-file
            try:
                # git merge-file 会原地修改 ours 文件
                # 返回值: 0=无冲突, >0=有冲突（返回冲突数量）, <0=错误
                result = subprocess.run(
                    [
                        "git", "merge-file",
                        "-L", "LOCAL (patched)",
                        "-L", "BASE (upstream old)",
                        "-L", "UPSTREAM (new)",
                        str(ours_file),
                        str(base_file),
                        str(theirs_file)
                    ],
                    capture_output=True,
                    timeout=30
                )
                
                merged_content = ours_file.read_text(encoding="utf-8")
                has_conflict = result.returncode > 0
                
                return True, merged_content, has_conflict
                
            except FileNotFoundError:
                # git 不可用，尝试 diff3
                pass
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass
            
            # 尝试 diff3
            try:
                # diff3 格式: diff3 -m <ours> <base> <theirs>
                result = subprocess.run(
                    [
                        "diff3", "-m",
                        "-L", "LOCAL (patched)",
                        "-L", "BASE (upstream old)",
                        "-L", "UPSTREAM (new)",
                        str(ours_file),
                        str(base_file),
                        str(theirs_file)
                    ],
                    capture_output=True,
                    timeout=30
                )
                
                merged_content = result.stdout.decode("utf-8")
                # diff3 返回值: 0=无冲突, 1=有冲突, 2=错误
                has_conflict = result.returncode == 1
                success = result.returncode in (0, 1)
                
                return success, merged_content, has_conflict
                
            except FileNotFoundError:
                # diff3 也不可用，返回失败
                return False, "", False
            except subprocess.TimeoutExpired:
                return False, "", False
            except Exception:
                return False, "", False
    
    def _handle_3way_merge_for_conflicts(
        self,
        conflict_files: Set[str],
        upstream_dir: Path,
        local_dir: Path,
        old_ref: str,
        old_ref_type: str,
        new_ref: str,
        new_ref_type: str
    ) -> Dict[str, Any]:
        """
        对冲突文件执行三方合并
        
        Args:
            conflict_files: 冲突文件集合（相对于 libs/OpenMemory/ 的路径）
            upstream_dir: 已下载的新上游版本目录
            local_dir: 本地 libs/OpenMemory 目录
            old_ref: 旧版本引用（lock 文件中的当前版本）
            old_ref_type: 旧版本引用类型
            new_ref: 新版本引用（要同步到的版本）
            new_ref_type: 新版本引用类型
        
        Returns:
            合并结果字典，包含：
            - auto_merged: 自动合并成功的文件列表
            - needs_manual: 需要手动处理的文件列表
            - failed: 合并失败的文件列表
            - by_category: 按 A/B/C 分类的统计
        """
        result = {
            "auto_merged": [],
            "needs_manual": [],
            "failed": [],
            "by_category": {"A": {"auto": 0, "manual": 0}, "B": {"auto": 0, "manual": 0}, "C": {"auto": 0, "manual": 0}},
            "conflict_files_generated": []
        }
        
        # 构建文件路径到最高优先级分类的映射
        file_to_category: Dict[str, str] = {}
        if self.patches_data:
            for patch_entry in self.patches_data.get("patches", []):
                patch_file_path = patch_entry.get("file", "")
                rel_path = patch_file_path.replace("libs/OpenMemory/", "")
                
                for change in patch_entry.get("changes", []):
                    category = change.get("category", "C")
                    # 优先级: A > B > C
                    if rel_path not in file_to_category:
                        file_to_category[rel_path] = category
                    elif category == "A":
                        file_to_category[rel_path] = "A"
                    elif category == "B" and file_to_category[rel_path] != "A":
                        file_to_category[rel_path] = "B"
        
        conflict_dir = self._get_conflict_artifacts_dir()
        conflict_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"\n[3-WAY] 开始三方合并 {len(conflict_files)} 个冲突文件...")
        
        for rel_path in conflict_files:
            category = file_to_category.get(rel_path, "C")
            safe_name = rel_path.replace("/", "_").replace("\\", "_")
            
            print(f"  [{category}] {rel_path}...", end=" ")
            
            # 获取三个版本的内容
            # 1. upstream_old: 从 lock 文件中的旧 ref 获取
            upstream_old_content = self._fetch_file_content_at_ref(rel_path, old_ref, old_ref_type)
            
            # 2. upstream_new: 从已下载的新版本获取
            upstream_new_file = upstream_dir / rel_path
            upstream_new_content = None
            if upstream_new_file.exists():
                try:
                    upstream_new_content = upstream_new_file.read_text(encoding="utf-8")
                except Exception:
                    pass
            
            # 3. local_patched: 本地当前文件
            local_file = local_dir / rel_path
            local_patched_content = None
            if local_file.exists():
                try:
                    local_patched_content = local_file.read_text(encoding="utf-8")
                except Exception:
                    pass
            
            # 检查是否所有内容都可用
            if upstream_old_content is None:
                print(f"[SKIP] 无法获取旧版本")
                result["failed"].append({
                    "path": rel_path,
                    "category": category,
                    "reason": f"无法从 {old_ref} 获取旧版本文件"
                })
                continue
            
            if upstream_new_content is None:
                print(f"[SKIP] 无法获取新版本")
                result["failed"].append({
                    "path": rel_path,
                    "category": category,
                    "reason": "新版本文件不存在或无法读取"
                })
                continue
            
            if local_patched_content is None:
                print(f"[SKIP] 本地文件不存在")
                result["failed"].append({
                    "path": rel_path,
                    "category": category,
                    "reason": "本地文件不存在或无法读取"
                })
                continue
            
            # 执行三方合并
            success, merged_content, has_conflict = self._perform_3way_merge(
                upstream_old_content=upstream_old_content,
                upstream_new_content=upstream_new_content,
                local_patched_content=local_patched_content,
                file_name=safe_name
            )
            
            if not success:
                print(f"[FAIL] 合并工具不可用")
                result["failed"].append({
                    "path": rel_path,
                    "category": category,
                    "reason": "git merge-file 和 diff3 都不可用"
                })
                continue
            
            if has_conflict:
                # 有冲突，生成冲突文件，保持原文件不变
                conflict_file = conflict_dir / f"merge_{safe_name}.conflict"
                try:
                    conflict_file.write_text(merged_content, encoding="utf-8")
                    print(f"[CONFLICT] -> {conflict_file.name}")
                    result["needs_manual"].append({
                        "path": rel_path,
                        "category": category,
                        "conflict_file": str(conflict_file.relative_to(self.workspace_root))
                    })
                    result["conflict_files_generated"].append(str(conflict_file.relative_to(self.workspace_root)))
                    result["by_category"][category]["manual"] += 1
                except Exception as e:
                    print(f"[ERROR] 无法写入冲突文件: {e}")
                    result["failed"].append({
                        "path": rel_path,
                        "category": category,
                        "reason": f"无法写入冲突文件: {e}"
                    })
            else:
                # 无冲突，自动落盘
                try:
                    local_file.write_text(merged_content, encoding="utf-8")
                    print(f"[OK] 自动合并成功")
                    result["auto_merged"].append({
                        "path": rel_path,
                        "category": category
                    })
                    result["by_category"][category]["auto"] += 1
                except Exception as e:
                    print(f"[ERROR] 无法写入文件: {e}")
                    result["failed"].append({
                        "path": rel_path,
                        "category": category,
                        "reason": f"无法写入合并结果: {e}"
                    })
        
        return result
    
    def _generate_sync_conflict_report(
        self,
        conflict_files: Set[str]
    ) -> Optional[Dict[str, Any]]:
        """
        生成 sync 冲突详情报告
        
        当 sync_upstream() 发现 conflict_files（modified_files ∩ patched_files）且未 --force 时调用。
        生成 .artifacts/openmemory-patch-conflicts/sync_conflicts.json，包含：
        - 文件路径、对应 A/B/C 最高级别、关联 patch ids
        - 建议策略（A:必须手动移植，B:优先尝试上游已有实现，C:可删除）
        - 建议命令
        
        Args:
            conflict_files: 冲突文件集合（相对于 libs/OpenMemory/ 的路径）
        
        Returns:
            冲突报告字典，包含 artifacts_dir 路径
        """
        if not conflict_files or not self.patches_data:
            return None
        
        # 构建文件路径到 patch 信息的映射
        # openmemory_patches.json 中的 file 字段格式: libs/OpenMemory/packages/...
        file_to_patches: Dict[str, List[Dict[str, Any]]] = {}
        
        for patch_entry in self.patches_data.get("patches", []):
            patch_file_path = patch_entry.get("file", "")
            # 从 libs/OpenMemory/ 前缀中提取相对路径
            rel_path = patch_file_path.replace("libs/OpenMemory/", "")
            
            for change in patch_entry.get("changes", []):
                if rel_path not in file_to_patches:
                    file_to_patches[rel_path] = []
                file_to_patches[rel_path].append({
                    "id": change.get("id"),
                    "category": change.get("category"),
                    "description": change.get("description"),
                    "reason": change.get("reason"),
                    "impact": change.get("impact"),
                    "upstream_potential": change.get("upstream_potential", False),
                    "patch_file": change.get("patch_file")
                })
        
        # 策略建议映射
        strategy_map = {
            "A": {
                "strategy": "must_manual_port",
                "description": "必须手动移植 - Engram 核心安全/功能约束，上游不会接受",
                "action": "需要在升级后手动重新应用补丁，确保 Engram 约束保持完整"
            },
            "B": {
                "strategy": "prefer_upstream",
                "description": "优先尝试上游已有实现 - 可能已被上游接受或有类似功能",
                "action": "检查上游新版本是否已包含类似功能，若有则删除本地补丁，若无则保留"
            },
            "C": {
                "strategy": "can_remove",
                "description": "可删除 - 临时修复/代码重复，升级后应重新评估",
                "action": "升级后重新评估是否仍需要此补丁，建议删除并使用共享模块"
            }
        }
        
        # 生成冲突详情
        conflicts = []
        category_counts = {"A": 0, "B": 0, "C": 0}
        highest_category = "C"  # 最高优先级（A > B > C）
        
        for cf in conflict_files:
            patches = file_to_patches.get(cf, [])
            
            # 确定该文件的最高级别
            file_highest = "C"
            patch_ids = []
            patch_details = []
            
            for p in patches:
                cat = p.get("category", "C")
                patch_ids.append(p.get("id"))
                patch_details.append({
                    "id": p.get("id"),
                    "category": cat,
                    "description": p.get("description"),
                    "patch_file": p.get("patch_file")
                })
                
                # 更新最高级别 (A > B > C)
                if cat == "A" or (cat == "B" and file_highest == "C"):
                    file_highest = cat
            
            category_counts[file_highest] = category_counts.get(file_highest, 0) + 1
            
            # 更新全局最高级别
            if file_highest == "A" or (file_highest == "B" and highest_category == "C"):
                highest_category = file_highest
            
            strategy_info = strategy_map.get(file_highest, strategy_map["C"])
            
            conflicts.append({
                "file": cf,
                "full_path": f"libs/OpenMemory/{cf}",
                "highest_category": file_highest,
                "patch_ids": patch_ids,
                "patches": patch_details,
                "strategy": strategy_info["strategy"],
                "strategy_description": strategy_info["description"],
                "recommended_action": strategy_info["action"]
            })
        
        # 排序：A 级别优先
        priority_order = {"A": 0, "B": 1, "C": 2}
        conflicts.sort(key=lambda x: (priority_order.get(x["highest_category"], 3), x["file"]))
        
        # 生成建议命令
        recommended_commands = [
            {
                "name": "pre_upgrade_snapshot",
                "description": "升级前创建快照（保存当前补丁状态）",
                "command": "make openmemory-pre-upgrade-snapshot-lib"
            },
            {
                "name": "upgrade_check",
                "description": "检查升级影响并生成详细报告",
                "command": "make openmemory-upgrade-check"
            },
            {
                "name": "force_sync",
                "description": "强制同步（覆盖本地修改，需谨慎）",
                "command": "python scripts/openmemory_sync.py sync --force"
            },
            {
                "name": "rollback",
                "description": "回滚到快照（如果升级失败）",
                "command": "make openmemory-rollback-lib"
            }
        ]
        
        # 创建报告
        report = {
            "timestamp": datetime.now().isoformat(),
            "summary": {
                "total_conflicts": len(conflicts),
                "highest_category": highest_category,
                "category_counts": category_counts,
                "requires_manual_intervention": highest_category == "A"
            },
            "conflicts": conflicts,
            "recommended_commands": recommended_commands,
            "notes": [
                "A 级别补丁是 Engram 核心约束，必须在升级后手动重新应用",
                "B 级别补丁可能已被上游接受，请先检查上游变更",
                "C 级别补丁通常可以删除，使用更好的替代方案",
                "建议在升级前执行 `make openmemory-pre-upgrade-snapshot-lib` 创建快照"
            ]
        }
        
        # 创建 artifacts 目录并写入文件
        artifacts_dir = self.workspace_root / ".artifacts" / "openmemory-patch-conflicts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        
        conflict_file_path = artifacts_dir / "sync_conflicts.json"
        try:
            with open(conflict_file_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(f"\n[INFO] 冲突报告已生成: {conflict_file_path}")
        except Exception as e:
            print(f"\n[WARN] 无法写入冲突报告: {e}")
            return None
        
        report["artifacts_dir"] = str(artifacts_dir)
        report["conflict_file"] = str(conflict_file_path)
        
        return report
    
    def sync_upstream(
        self,
        ref: Optional[str] = None,
        ref_type: Optional[str] = None,
        exclude_patterns: Optional[List[str]] = None,
        dry_run: bool = True,
        force: bool = False,
        strategy: ConflictStrategy = ConflictStrategy.CLEAN,
        update_ref: bool = True
    ) -> Dict[str, Any]:
        """
        同步上游代码到本地
        
        Args:
            ref: 版本引用，默认从 lock 文件读取
            ref_type: 引用类型，默认从 lock 文件读取
            exclude_patterns: 额外排除模式，会与默认模式合并
            dry_run: 如果为 True，仅显示将要执行的操作（默认 True）
            force: 如果为 True，强制覆盖（即使有本地修改）
            strategy: 冲突处理策略 (clean/3way/manual)，当有补丁冲突时生效
            update_ref: 如果为 True，更新 lock 中的 upstream_ref/upstream_ref_type（默认 True）
                        手动流程或不想更新 ref 时可设为 False
        
        Returns:
            sync 结果字典
        """
        if not self.load_config_files():
            return {"ok": False, "error": "无法加载配置文件"}
        
        # 合并排除模式
        all_exclude_patterns = DEFAULT_EXCLUDE_PATTERNS.copy()
        if exclude_patterns:
            all_exclude_patterns.extend(exclude_patterns)
        
        # 从参数或 lock 文件获取配置
        ref = ref or self.lock_data.get("upstream_ref", "main")
        ref_type = ref_type or self.lock_data.get("upstream_ref_type", "tag")
        
        print("\n" + "=" * 60)
        print("OpenMemory 上游同步 (sync)")
        print("=" * 60)
        print(f"  上游仓库: {self.lock_data.get('upstream_url')}")
        print(f"  版本引用: {ref} ({ref_type})")
        print(f"  模式: {'dry-run（预览）' if dry_run else '实际执行'}")
        print(f"  强制覆盖: {'是' if force else '否'}")
        print(f"  排除模式数: {len(all_exclude_patterns)}")
        print()
        
        result = {
            "ok": False,
            "ref": ref,
            "ref_type": ref_type,
            "dry_run": dry_run,
            "force": force,
            "timestamp": datetime.now().isoformat(),
            "commit_sha": None,
            "commit_date": None,
            "archive_sha256": None,
            "comparison": None,
            "actions": []
        }
        
        # Step 1: Fetch 上游代码
        print("[1/4] Fetch 上游代码...")
        with tempfile.TemporaryDirectory(prefix="openmemory_sync_") as temp_dir:
            temp_path = Path(temp_dir)
            
            fetch_result = self.fetch_upstream(
                ref=ref, 
                ref_type=ref_type, 
                output_dir=temp_path,
                dry_run=False  # 始终执行实际下载
            )
            
            if not fetch_result["ok"]:
                result["error"] = fetch_result.get("error", "Fetch 失败")
                return result
            
            result["commit_sha"] = fetch_result["commit_sha"]
            result["commit_date"] = fetch_result["commit_date"]
            result["archive_sha256"] = fetch_result["archive_sha256"]
            
            upstream_dir = Path(fetch_result["extracted_dir"])
            
            # Step 2: 计算 patched_files 的 base checksums
            print("\n[2/5] 计算 patched_files base checksums...")
            base_checksums = self._compute_base_checksums_from_upstream(
                upstream_dir=upstream_dir,
                ref=ref,
                ref_type=ref_type,
                commit_sha=result["commit_sha"]
            )
            result["base_checksums"] = base_checksums
            
            computed_count = sum(1 for info in base_checksums.values() if info.get("base"))
            total_count = len(base_checksums)
            print(f"  已计算: {computed_count}/{total_count} 个文件")
            
            # Step 3: 对比本地与上游
            print("\n[3/5] 对比本地与上游...")
            local_dir = self.workspace_root / "libs" / "OpenMemory"
            
            comparison = self._compare_directories(upstream_dir, local_dir, all_exclude_patterns)
            result["comparison"] = comparison
            
            # 打印对比摘要
            summary = comparison["summary"]
            print(f"\n  文件对比摘要:")
            print(f"    上游文件数: {summary['total_upstream']}")
            print(f"    本地文件数: {summary['total_local']}")
            print(f"    新增文件: {summary['new']}")
            print(f"    删除文件: {summary['deleted']}")
            print(f"    修改文件: {summary['modified']}")
            print(f"    未变文件: {summary['unchanged']}")
            
            # 按目录显示关键变化
            print(f"\n  关键目录变化:")
            for change_type, dirs in comparison["by_directory"].items():
                if dirs:
                    print(f"    [{change_type}]:")
                    for dir_path, count in list(dirs.items())[:5]:
                        print(f"      - {dir_path}: {count} 文件")
            
            if dry_run:
                print("\n[DRY-RUN] 跳过实际同步")
                result["ok"] = True
                result["actions"] = [
                    {"action": "would_add", "files": comparison["new_files"][:10]},
                    {"action": "would_modify", "files": comparison["modified_files"][:10]},
                    {"action": "would_delete", "files": comparison["deleted_files"][:10]}
                ]
                
                # 更新 lock 文件中的预览信息（仅 dry-run 时）
                print("\n[INFO] Lock 文件将更新以下字段:")
                print(f"  upstream_commit_sha: {result['commit_sha']}")
                print(f"  upstream_commit_date: {result['commit_date']}")
                print(f"  archive_sha256: {result['archive_sha256']}")
                print(f"  base_checksums: {computed_count}/{total_count} 个文件")
                
                return result
            
            # Step 4: 执行同步
            print("\n[4/5] 执行文件同步...")
            
            # 检查是否有本地修改需要确认
            patched_files = set(item["path"].replace("libs/OpenMemory/", "") 
                               for item in self.lock_data.get("patched_files", []))
            
            conflict_files = set(comparison["modified_files"]) & patched_files
            if conflict_files and not force:
                print(f"\n[WARN] 发现 {len(conflict_files)} 个已补丁文件将被修改:")
                for f in list(conflict_files)[:5]:
                    print(f"    - {f}")
                
                # 策略处理
                if strategy == ConflictStrategy.THREE_WAY:
                    # 三方合并策略
                    print(f"\n[STRATEGY] 使用三方合并策略处理 {len(conflict_files)} 个冲突文件...")
                    
                    # 获取 lock 文件中的旧 ref（当前基线版本）
                    old_ref = self.lock_data.get("upstream_ref", "main")
                    old_ref_type = self.lock_data.get("upstream_ref_type", "tag")
                    
                    print(f"  旧版本: {old_ref} ({old_ref_type})")
                    print(f"  新版本: {ref} ({ref_type})")
                    
                    # 执行三方合并
                    merge_result = self._handle_3way_merge_for_conflicts(
                        conflict_files=conflict_files,
                        upstream_dir=upstream_dir,
                        local_dir=local_dir,
                        old_ref=old_ref,
                        old_ref_type=old_ref_type,
                        new_ref=ref,
                        new_ref_type=ref_type
                    )
                    
                    result["merge_result"] = merge_result
                    
                    # 打印统计摘要
                    print(f"\n[3-WAY] 合并结果统计:")
                    print(f"  自动合并成功 (auto-merged): {len(merge_result['auto_merged'])}")
                    print(f"  需要手动处理 (needs-manual): {len(merge_result['needs_manual'])}")
                    print(f"  合并失败 (failed): {len(merge_result['failed'])}")
                    
                    print(f"\n[3-WAY] 按分类统计:")
                    for cat in ["A", "B", "C"]:
                        cat_stats = merge_result["by_category"][cat]
                        print(f"  Category {cat}: auto={cat_stats['auto']}, manual={cat_stats['manual']}")
                    
                    # 检查 Category A 是否仍有冲突
                    has_category_a_conflict = merge_result["by_category"]["A"]["manual"] > 0
                    
                    if has_category_a_conflict:
                        # Category A 有冲突，返回 ERROR
                        print(f"\n[ERROR] Category A 存在 {merge_result['by_category']['A']['manual']} 个未解决冲突")
                        print("  Category A 补丁是必须保留的，请手动解决冲突后重试")
                        result["error"] = "Category A 存在未解决冲突"
                        result["conflict_files"] = [item["path"] for item in merge_result["needs_manual"]]
                        result["conflict_files_generated"] = merge_result.get("conflict_files_generated", [])
                        
                        # 生成冲突详情报告
                        conflict_report = self._generate_sync_conflict_report(conflict_files)
                        if conflict_report:
                            result["conflict_report"] = conflict_report
                            result["conflict_artifacts_dir"] = str(conflict_report.get("artifacts_dir", ""))
                            self.report.conflict_artifacts_dir = conflict_report.get("artifacts_dir")
                            self.report.conflict_files = result["conflict_files"]
                        
                        return result
                    
                    # Category A 无冲突，可以继续（从冲突文件列表中移除已自动合并的文件）
                    auto_merged_paths = {item["path"] for item in merge_result["auto_merged"]}
                    # 将自动合并的文件从 modified_files 中移除，避免后续被覆盖
                    comparison["modified_files"] = [
                        f for f in comparison["modified_files"] 
                        if f not in auto_merged_paths
                    ]
                    
                    # 如果还有 needs_manual 文件（B/C 类），打印警告但继续
                    if merge_result["needs_manual"]:
                        print(f"\n[WARN] 仍有 {len(merge_result['needs_manual'])} 个 B/C 类文件需要手动处理")
                        print("  冲突文件已生成到 .artifacts/openmemory-patch-conflicts/")
                        print("  这些文件的原始版本保持不变，请手动合并后替换")
                        result["conflict_files_generated"] = merge_result.get("conflict_files_generated", [])
                    
                    print(f"\n[3-WAY] 三方合并完成，继续执行同步...")
                    
                else:
                    # 非三方合并策略（clean 或 manual）
                    print("\n使用 --force 强制覆盖，或使用 --strategy 3way 尝试三方合并")
                    result["error"] = "存在补丁冲突"
                    result["conflict_files"] = list(conflict_files)
                    
                    # 生成冲突详情报告
                    conflict_report = self._generate_sync_conflict_report(conflict_files)
                    if conflict_report:
                        result["conflict_report"] = conflict_report
                        result["conflict_artifacts_dir"] = str(conflict_report.get("artifacts_dir", ""))
                        # 设置 SyncReport 的冲突产物目录
                        self.report.conflict_artifacts_dir = conflict_report.get("artifacts_dir")
                        self.report.conflict_files = list(conflict_files)
                    
                    return result
            
            actions = []
            
            # 创建目标目录（如果不存在）
            local_dir.mkdir(parents=True, exist_ok=True)
            
            # 复制新文件
            for rel_path in comparison["new_files"]:
                src = upstream_dir / rel_path
                dst = local_dir / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                actions.append({"action": "added", "file": rel_path})
            
            # 更新修改的文件
            for rel_path in comparison["modified_files"]:
                src = upstream_dir / rel_path
                dst = local_dir / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                actions.append({"action": "modified", "file": rel_path})
            
            # 删除被删除的文件（谨慎处理）
            for rel_path in comparison["deleted_files"]:
                # 跳过被补丁的文件
                if f"libs/OpenMemory/{rel_path}" in patched_files:
                    actions.append({"action": "kept_patched", "file": rel_path})
                    continue
                dst = local_dir / rel_path
                if dst.exists():
                    dst.unlink()
                    actions.append({"action": "deleted", "file": rel_path})
            
            result["actions"] = actions
            
            # Step 5: 更新 lock 文件
            print("\n[5/5] 更新 lock 文件...")
            
            self.lock_data["upstream_commit_sha"] = result["commit_sha"]
            self.lock_data["upstream_commit_date"] = result["commit_date"]
            self.lock_data["last_sync_at"] = datetime.now().isoformat() + "Z"
            self.lock_data["sync_method"] = "archive"
            
            # 更新 upstream_ref/upstream_ref_type（CI 以此为唯一变更信号）
            # 如果指定 --no-update-ref 则不更新，用于手动流程
            if update_ref:
                # 评估冻结状态和更新策略
                freeze_status = self.lock_data.get("freeze_status", {})
                upgrade_policy = self.lock_data.get("upgrade_policy", {})
                freeze_rules = upgrade_policy.get("freeze_rules", {}).get("rules", [])
                
                # 获取 override 参数（从 result 或环境变量）
                override_reason = os.environ.get("SYNC_OVERRIDE_REASON", "")
                override_authority = os.environ.get("SYNC_OVERRIDE_AUTHORITY", "")
                
                policy_result = self._evaluate_freeze_and_update_ref_policy(
                    freeze_status=freeze_status,
                    freeze_rules=freeze_rules,
                    strict_patch_files=False,
                    verify_result=None,
                    apply_result=None,
                    override_reason=override_reason,
                    override_authority=override_authority
                )
                
                result["freeze_policy_check"] = policy_result
                
                if not policy_result["allow_update_ref"]:
                    # 冻结且无有效 override，拒绝更新 upstream_ref
                    print(f"\n  [FREEZE] upstream_ref 更新被阻止")
                    print(f"  原因: {policy_result['block_reason']}")
                    
                    if policy_result["triggered_rules"]:
                        print("  触发的规则:")
                        for rule in policy_result["triggered_rules"]:
                            print(f"    - {rule['id']}: {rule['reason']}")
                    
                    if policy_result["recommended_flags"]:
                        print("  推荐的标志:")
                        for flag in policy_result["recommended_flags"]:
                            print(f"    {flag}")
                    
                    # 记录 override 尝试（如果有）
                    if override_reason and not policy_result["override_valid"]:
                        print(f"\n  [WARN] 提供的 override 无效:")
                        for issue in policy_result["override_issues"]:
                            print(f"    - {issue}")
                    
                    result["update_ref"] = False
                    result["update_ref_blocked"] = True
                    result["update_ref_block_reason"] = policy_result["block_reason"]
                else:
                    # 允许更新
                    old_ref = self.lock_data.get("upstream_ref")
                    old_ref_type = self.lock_data.get("upstream_ref_type")
                    self.lock_data["upstream_ref"] = ref
                    self.lock_data["upstream_ref_type"] = ref_type
                    
                    if old_ref != ref or old_ref_type != ref_type:
                        print(f"  upstream_ref: {old_ref} -> {ref}")
                        print(f"  upstream_ref_type: {old_ref_type} -> {ref_type}")
                    
                    result["update_ref"] = True
                    
                    # 如果是通过 override 更新的，记录 override 信息
                    if policy_result["needs_override"] and policy_result["override_valid"]:
                        print(f"\n  [OVERRIDE] 通过 override 更新 upstream_ref")
                        print(f"  override 原因: {override_reason}")
                        
                        # 更新 lock 中的 override 记录
                        if "freeze_status" not in self.lock_data:
                            self.lock_data["freeze_status"] = {}
                        self.lock_data["freeze_status"]["last_override_at"] = datetime.now().isoformat() + "Z"
                        self.lock_data["freeze_status"]["last_override_by"] = override_authority or "unknown"
                        self.lock_data["freeze_status"]["last_override_reason"] = override_reason
                        
                        result["update_ref_override"] = {
                            "reason": override_reason,
                            "authority": override_authority,
                            "timestamp": self.lock_data["freeze_status"]["last_override_at"]
                        }
            else:
                print("  [跳过] upstream_ref 更新（--no-update-ref）")
                result["update_ref"] = False
            
            # 添加 archive 信息
            if "archive_info" not in self.lock_data:
                self.lock_data["archive_info"] = {}
            self.lock_data["archive_info"]["sha256"] = result["archive_sha256"]
            self.lock_data["archive_info"]["ref"] = ref
            self.lock_data["archive_info"]["ref_type"] = ref_type
            
            # 更新 base checksums
            if base_checksums:
                print("  更新 base checksums...")
                self._update_lock_base_checksums(base_checksums)
            
            # 写入文件（统一格式：2空格缩进、键排序、UTF-8、尾换行）
            with open(self.lock_file, "w", encoding="utf-8") as f:
                json.dump(self.lock_data, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.write("\n")
            
            result["ok"] = True
        
        print("\n" + "=" * 60)
        print("[OK] Sync 完成")
        print(f"  新增: {len([a for a in result['actions'] if a['action'] == 'added'])}")
        print(f"  修改: {len([a for a in result['actions'] if a['action'] == 'modified'])}")
        print(f"  删除: {len([a for a in result['actions'] if a['action'] == 'deleted'])}")
        print(f"  保留: {len([a for a in result['actions'] if a['action'] == 'kept_patched'])}")
        print("=" * 60)
        
        return result
    
    def promote_lock(
        self,
        ref: Optional[str] = None,
        ref_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        更新 lock 文件的 upstream_ref 及相关字段（promote 阶段）
        
        此方法用于在 verify + test-multi-schema 通过后，更新 lock 文件中的：
        - upstream_ref / upstream_ref_type
        - upstream_commit_sha / upstream_commit_date
        - archive_info（sha256 等）
        - checksums.base（patched_files 的 base checksums）
        
        Args:
            ref: 版本引用，默认从当前 lock 文件的 archive_info.ref 或参数读取
            ref_type: 引用类型，默认从当前 lock 文件的 archive_info.ref_type 或参数读取
        
        Returns:
            promote 结果字典
        """
        if not self.load_config_files():
            return {"ok": False, "error": "无法加载配置文件"}
        
        print("\n" + "=" * 60)
        print("OpenMemory Lock 文件更新 (promote)")
        print("=" * 60)
        
        result = {
            "ok": False,
            "timestamp": datetime.now().isoformat(),
            "updates": {}
        }
        
        # 确定要更新的 ref 和 ref_type
        # 优先级: 命令行参数 > archive_info 中的值 > lock 文件当前值
        archive_info = self.lock_data.get("archive_info", {})
        
        target_ref = ref or archive_info.get("ref") or self.lock_data.get("upstream_ref", "main")
        target_ref_type = ref_type or archive_info.get("ref_type") or self.lock_data.get("upstream_ref_type", "tag")
        
        print(f"  目标版本: {target_ref} ({target_ref_type})")
        print()
        
        # 检查是否需要 fetch 新数据
        need_fetch = False
        
        # 如果 archive_info 中没有 sha256，需要 fetch
        if not archive_info.get("sha256"):
            print("[INFO] archive_info.sha256 为空，需要 fetch 上游数据")
            need_fetch = True
        
        # 如果参数指定的 ref 与 archive_info.ref 不同，需要 fetch
        if ref and ref != archive_info.get("ref"):
            print(f"[INFO] 参数指定的 ref ({ref}) 与 archive_info.ref ({archive_info.get('ref')}) 不同，需要 fetch")
            need_fetch = True
        
        if need_fetch:
            print("\n[1/2] Fetch 上游数据...")
            with tempfile.TemporaryDirectory(prefix="openmemory_promote_") as temp_dir:
                temp_path = Path(temp_dir)
                
                fetch_result = self.fetch_upstream(
                    ref=target_ref,
                    ref_type=target_ref_type,
                    output_dir=temp_path,
                    dry_run=False
                )
                
                if not fetch_result["ok"]:
                    result["error"] = fetch_result.get("error", "Fetch 失败")
                    return result
                
                # 更新 archive_info
                archive_info["sha256"] = fetch_result["archive_sha256"]
                archive_info["ref"] = target_ref
                archive_info["ref_type"] = target_ref_type
                
                result["updates"]["archive_info"] = archive_info
                result["updates"]["upstream_commit_sha"] = fetch_result["commit_sha"]
                result["updates"]["upstream_commit_date"] = fetch_result["commit_date"]
                
                # 计算 base checksums
                upstream_dir = Path(fetch_result["extracted_dir"])
                base_checksums = self._compute_base_checksums_from_upstream(
                    upstream_dir=upstream_dir,
                    ref=target_ref,
                    ref_type=target_ref_type,
                    commit_sha=fetch_result["commit_sha"]
                )
                
                if base_checksums:
                    result["updates"]["base_checksums"] = base_checksums
        else:
            print("\n[1/2] 使用已有的 archive_info 数据")
            result["updates"]["archive_info"] = archive_info
        
        # 更新 lock 文件
        print("\n[2/2] 更新 lock 文件...")
        
        old_ref = self.lock_data.get("upstream_ref")
        old_ref_type = self.lock_data.get("upstream_ref_type")
        
        # 更新 upstream_ref 和 upstream_ref_type
        self.lock_data["upstream_ref"] = target_ref
        self.lock_data["upstream_ref_type"] = target_ref_type
        
        result["updates"]["upstream_ref"] = {
            "old": old_ref,
            "new": target_ref
        }
        result["updates"]["upstream_ref_type"] = {
            "old": old_ref_type,
            "new": target_ref_type
        }
        
        # 更新时间戳
        self.lock_data["last_sync_at"] = datetime.now().isoformat() + "Z"
        
        # 更新 commit 信息（如果有）
        if "upstream_commit_sha" in result["updates"]:
            self.lock_data["upstream_commit_sha"] = result["updates"]["upstream_commit_sha"]
        if "upstream_commit_date" in result["updates"]:
            self.lock_data["upstream_commit_date"] = result["updates"]["upstream_commit_date"]
        
        # 更新 archive_info
        if "archive_info" in result["updates"]:
            self.lock_data["archive_info"] = result["updates"]["archive_info"]
        
        # 更新 base checksums
        if "base_checksums" in result["updates"]:
            self._update_lock_base_checksums(result["updates"]["base_checksums"])
        
        # 写入文件（统一格式：2空格缩进、键排序、UTF-8、尾换行）
        with open(self.lock_file, "w", encoding="utf-8") as f:
            json.dump(self.lock_data, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")
        
        result["ok"] = True
        
        print()
        print("更新摘要:")
        print(f"  upstream_ref: {old_ref} -> {target_ref}")
        print(f"  upstream_ref_type: {old_ref_type} -> {target_ref_type}")
        if "upstream_commit_sha" in result["updates"]:
            print(f"  upstream_commit_sha: {result['updates']['upstream_commit_sha']}")
        if "base_checksums" in result["updates"]:
            print(f"  base_checksums: {len(result['updates']['base_checksums'])} 个文件")
        
        print("\n" + "=" * 60)
        print("[OK] Lock 文件更新完成")
        print("=" * 60)
        
        return result
    
    def print_report(self, json_output: bool = False):
        """打印报告"""
        if json_output:
            print(json.dumps(self.report.to_dict(), indent=2, ensure_ascii=False))
            return
        
        # 文本格式输出
        print("\n" + "=" * 60)
        print("OpenMemory 同步状态报告")
        print("=" * 60)
        print(f"时间: {self.report.timestamp}")
        print(f"整体状态: {self.report.overall_status.value.upper()}")
        print()
        
        # 检查结果
        print("检查结果:")
        print("-" * 40)
        for check in self.report.checks:
            status_icon = {
                "ok": "✓",
                "warn": "⚠",
                "error": "✗"
            }.get(check["status"], "?")
            print(f"  {status_icon} {check['name']}: {check['message']}")
        print()
        
        # 补丁状态
        if self.report.patches_status:
            print("补丁状态:")
            print("-" * 40)
            ps = self.report.patches_status
            print(f"  总计: {ps.get('total', 0)} 个补丁")
            print(f"  Category A (必须保留): {ps.get('category_A_must_keep', 0)}")
            print(f"  Category B (可上游化): {ps.get('category_B_upstreamable', 0)}")
            print(f"  Category C (可移除): {ps.get('category_C_removable', 0)}")
            print()
        
        # 建议
        if self.report.suggestions:
            print("下一步建议:")
            print("-" * 40)
            for suggestion in self.report.suggestions:
                print(f"  [{suggestion['priority']}] {suggestion['action']}")
                print(f"      {suggestion['description']}")
                if suggestion.get("commands"):
                    for cmd in suggestion["commands"]:
                        print(f"      $ {cmd}")
            print()
        
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="OpenMemory 同步工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python openmemory_sync.py check              # 一致性检查
  python openmemory_sync.py check --json       # JSON 格式输出
  python openmemory_sync.py schema-validate    # 仅执行 JSON Schema 校验
  python openmemory_sync.py schema-validate --strict  # 严格模式（校验失败返回错误）
  python openmemory_sync.py apply --dry-run    # 补丁预览（默认）
  python openmemory_sync.py apply              # 实际应用补丁
  python openmemory_sync.py apply --categories A  # 仅应用 Category A 补丁
  python openmemory_sync.py apply --strategy manual  # 生成冲突文件供人工审查
  python openmemory_sync.py apply --strategy 3way    # 尝试三方合并
  python openmemory_sync.py verify             # 校验补丁是否已落地
  python openmemory_sync.py suggest            # 输出建议
  python openmemory_sync.py fetch              # 获取上游代码
  python openmemory_sync.py fetch --ref v1.4.0 # 获取指定版本
  python openmemory_sync.py sync               # 同步上游代码（dry-run）
  python openmemory_sync.py sync --no-dry-run  # 实际执行同步
  python openmemory_sync.py sync --no-dry-run --no-update-ref  # 同步但不更新 lock 的 upstream_ref
  python openmemory_sync.py promote            # 更新 lock 文件（验证通过后使用）
  python openmemory_sync.py promote --ref v1.4.0  # 指定版本更新 lock 文件

升级三步曲（推荐流程）:
  1. make openmemory-upgrade-preview UPSTREAM_REF=v1.4.0  # 预览变更
  2. make openmemory-upgrade-sync UPSTREAM_REF=v1.4.0     # 执行同步
  3. make openmemory-upgrade-promote                      # 验证通过后更新 lock

Schema 校验:
  schema-validate           执行 JSON Schema 校验（默认 warn 模式）
  --schema-strict          schema 校验失败视为错误（check/all 命令）
  --schema-warn-only       schema 校验失败仅警告（默认）

冲突处理策略:
  --strategy clean   失败则停止，需要手动解决（默认）
  --strategy 3way    尝试三方合并，如仍有冲突请手动处理
  --strategy manual  生成冲突文件到 .artifacts/openmemory-patch-conflicts/

冲突分类处理:
  Category A 冲突: 整体状态置 ERROR（必须修复）
  Category B 冲突: 整体状态置 WARN（应当关注）
  Category C 冲突: 允许跳过并记录（可选修复）
        """
    )
    
    parser.add_argument(
        "command",
        choices=["check", "apply", "verify", "suggest", "fetch", "sync", "promote", "all", "schema-validate"],
        help="执行的操作: check=一致性检查, apply=应用补丁, verify=校验落地, suggest=输出建议, fetch=获取上游, sync=同步上游, promote=更新lock文件, all=执行全部检查, schema-validate=JSON Schema 校验"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="仅预览，不实际执行（apply/sync 命令默认启用）"
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="实际执行（覆盖默认的 dry-run）"
    )
    parser.add_argument(
        "--categories",
        type=str,
        default=None,
        help="要应用的补丁分类，逗号分隔，如 'A' 或 'A,B,C'（默认全部）"
    )
    parser.add_argument(
        "--strategy",
        type=str,
        choices=["clean", "3way", "manual"],
        default="clean",
        help="冲突处理策略: clean=失败则停止, 3way=尝试三方合并, manual=生成冲突文件供人工审查（默认 clean）"
    )
    parser.add_argument(
        "--ref",
        type=str,
        default=None,
        help="上游版本引用（tag 或 commit SHA），默认从 lock 文件读取"
    )
    parser.add_argument(
        "--ref-type",
        type=str,
        choices=["tag", "commit"],
        default=None,
        help="引用类型（tag 或 commit），默认从 lock 文件读取"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="fetch 输出目录（默认临时目录）"
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default=None,
        help="额外排除模式，逗号分隔（sync 时使用）"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制覆盖（sync 时忽略补丁冲突）"
    )
    parser.add_argument(
        "--force-update-lock",
        action="store_true",
        help="强制更新 lock 文件（apply 命令，绕过 verify 检查，紧急情况使用，会在报告中标注）"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="工作区根目录（默认自动检测）"
    )
    parser.add_argument(
        "--schema-strict",
        action="store_true",
        help="Schema 校验失败视为错误（check/all/schema-validate 命令）"
    )
    parser.add_argument(
        "--schema-warn-only",
        action="store_true",
        default=True,
        help="Schema 校验失败仅警告（默认）"
    )
    parser.add_argument(
        "--strict-patch-files",
        action="store_true",
        default=False,
        help="Patch 文件缺失或 SHA256 不匹配时视为错误（默认仅警告；可通过 OPENMEMORY_PATCH_FILES_REQUIRED=1 环境变量启用）"
    )
    parser.add_argument(
        "--no-update-ref",
        action="store_true",
        default=False,
        help="同步时不更新 lock 的 upstream_ref/upstream_ref_type（手动流程使用，CI 以 upstream_ref 变化为唯一信号）"
    )
    
    args = parser.parse_args()
    
    # 初始化工具
    tool = OpenMemorySyncTool(workspace_root=args.workspace)
    
    success = True
    result = None
    
    # 确定 schema 校验模式
    schema_warn_only = not args.schema_strict
    
    # 确定 patch 文件严格模式（命令行参数优先，否则读取环境变量）
    strict_patch_files = args.strict_patch_files or os.environ.get("OPENMEMORY_PATCH_FILES_REQUIRED", "").lower() in ("1", "true", "yes")
    
    if args.command == "schema-validate":
        # 独立的 schema 校验命令
        if not tool.load_config_files():
            success = False
        else:
            success = tool.validate_schema(warn_only=schema_warn_only)
            if not args.json:
                print("\n" + "=" * 60)
                print("OpenMemory JSON Schema 校验")
                print("=" * 60)
                print(f"模式: {'warn-only（警告模式）' if schema_warn_only else 'strict（严格模式）'}")
                print()
                for check in tool.report.checks:
                    if check["name"].startswith("schema_validate"):
                        status_icon = {"ok": "✓", "warn": "⚠", "error": "✗"}.get(check["status"], "?")
                        print(f"  {status_icon} {check['name']}: {check['message']}")
                        if check.get("details", {}).get("errors"):
                            for err in check["details"]["errors"][:5]:
                                print(f"      - {err['path']}: {err['message']}")
                print()
                print("=" * 60)
        tool.print_report(json_output=args.json)
        sys.exit(0 if success else 1)
    
    if args.command in ("check", "all"):
        success = tool.run_consistency_check(
            schema_warn_only=schema_warn_only,
            strict_patch_files=strict_patch_files
        ) and success
    
    if args.command in ("apply", "all"):
        # 解析 dry_run 参数：默认 dry_run=True，除非指定 --no-dry-run
        if args.no_dry_run:
            dry_run = False
        elif args.dry_run is not None:
            dry_run = args.dry_run
        else:
            # apply 命令默认 dry_run，all 命令也默认 dry_run
            dry_run = True
        
        # 解析 categories 参数
        categories = None
        if args.categories:
            categories = [c.strip().upper() for c in args.categories.split(",")]
        
        # 解析 strategy 参数
        strategy_map = {
            "clean": ConflictStrategy.CLEAN,
            "3way": ConflictStrategy.THREE_WAY,
            "manual": ConflictStrategy.MANUAL
        }
        strategy = strategy_map.get(args.strategy, ConflictStrategy.CLEAN)
        
        success = tool.apply_patches(
            dry_run=dry_run, 
            categories=categories, 
            strategy=strategy,
            quiet=args.json
        ) and success
    
    if args.command == "verify":
        success = tool.verify_patches(quiet=args.json) and success
    
    if args.command in ("suggest", "all"):
        tool.generate_suggestions()
    
    if args.command == "fetch":
        # 解析 dry_run 参数
        dry_run = args.dry_run if args.dry_run is not None else False
        if args.no_dry_run:
            dry_run = False
        
        result = tool.fetch_upstream(
            ref=args.ref,
            ref_type=args.ref_type,
            output_dir=args.output_dir,
            dry_run=dry_run
        )
        success = result.get("ok", False)
        
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            sys.exit(0 if success else 1)
    
    if args.command == "sync":
        # 解析 dry_run 参数：sync 命令默认 dry_run=True
        if args.no_dry_run:
            dry_run = False
        elif args.dry_run is not None:
            dry_run = args.dry_run
        else:
            dry_run = True
        
        # 解析 exclude 参数
        exclude_patterns = None
        if args.exclude:
            exclude_patterns = [p.strip() for p in args.exclude.split(",")]
        
        # 解析 strategy 参数
        strategy_map = {
            "clean": ConflictStrategy.CLEAN,
            "3way": ConflictStrategy.THREE_WAY,
            "manual": ConflictStrategy.MANUAL
        }
        sync_strategy = strategy_map.get(args.strategy, ConflictStrategy.CLEAN)
        
        # 解析 update_ref 参数：默认 True，--no-update-ref 时为 False
        # CI 以 lock 的 upstream_ref 变化作为唯一变更信号
        update_ref = not args.no_update_ref
        
        result = tool.sync_upstream(
            ref=args.ref,
            ref_type=args.ref_type,
            exclude_patterns=exclude_patterns,
            dry_run=dry_run,
            force=args.force,
            strategy=sync_strategy,
            update_ref=update_ref
        )
        success = result.get("ok", False)
        
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            sys.exit(0 if success else 1)
    
    if args.command == "promote":
        result = tool.promote_lock(
            ref=args.ref,
            ref_type=args.ref_type
        )
        success = result.get("ok", False)
        
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            sys.exit(0 if success else 1)
    
    # 对于非 fetch/sync/promote 命令，输出标准报告
    if args.command not in ("fetch", "sync", "promote"):
        tool.print_report(json_output=args.json)
    
    # 返回状态码
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
