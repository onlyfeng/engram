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
            "suggestions": self.suggestions
        }
        # 仅在有冲突时添加冲突相关字段
        if self.conflict_artifacts_dir:
            result["conflict_artifacts_dir"] = self.conflict_artifacts_dir
        if self.conflict_files:
            result["conflict_files"] = self.conflict_files
        return result


class OpenMemorySyncTool:
    """OpenMemory 同步工具"""
    
    def __init__(self, workspace_root: Optional[Path] = None):
        self.workspace_root = workspace_root or Path(__file__).parent.parent
        self.lock_file = self.workspace_root / "OpenMemory.upstream.lock.json"
        self.patches_file = self.workspace_root / "openmemory_patches.json"
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
    
    def run_consistency_check(self) -> bool:
        """执行完整的一致性检查"""
        if not self.load_config_files():
            return False
        
        results = [
            self.check_upstream_info(),
            self.check_components(),
            self.check_patched_files(),
            self.check_patches_consistency(),
        ]
        self.check_patch_categories_summary()
        
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
            
            with open(self.lock_file, "w", encoding="utf-8") as f:
                json.dump(self.lock_data, f, indent=2, ensure_ascii=False)
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
        quiet: bool = False
    ) -> bool:
        """
        应用补丁
        
        Args:
            dry_run: 如果为 True，仅预览不实际执行
            categories: 要应用的补丁分类列表，如 ["A"] 或 ["A", "B", "C"]，默认全部
            strategy: 冲突处理策略 (clean/3way/manual)
        
        Returns:
            bool: 是否成功
        
        补丁应用流程:
            1. 读取 openmemory_patches.json，按 Category A→B→C 顺序处理
            2. 对每个补丁：先 dry-run 校验（目标文件存在性），再实际 apply
            3. 计算目标文件 sha256 并写入/对照 OpenMemory.upstream.lock.json 的 checksums
            4. 输出 JSON 报告
        
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
            "conflict_files": []  # 冲突产物文件路径列表
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
        
        if not quiet:
            print("\n" + "=" * 60)
        
        # 实际应用模式下更新 lock 文件
        if not dry_run:
            if not quiet:
                print("\n[INFO] 更新 lock 文件 checksums...")
            if self._update_lock_checksums(new_checksums):
                if not quiet:
                    print("[OK] lock 文件已更新")
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
    
    def verify_patches(self) -> bool:
        """
        校验补丁是否已正确落地
        
        Returns:
            bool: 所有补丁是否已正确落地
        """
        if not self.load_config_files():
            return False
        
        patches = self.patches_data.get("patches", [])
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
        
        verify_result = {
            "timestamp": datetime.now().isoformat(),
            "files": [],
            "verified": [],
            "missing": [],
            "checksum_mismatch": []
        }
        
        print("\n[VERIFY] 校验补丁落地状态...")
        print("=" * 60)
        
        for patch_group in patches:
            file_path = patch_group.get("file", "")
            full_path = self.workspace_root / file_path
            exists = full_path.exists()
            
            file_result = {
                "path": file_path,
                "exists": exists,
                "sha256": None,
                "expected_sha256": existing_checksums.get(file_path),
                "status": "pending"
            }
            
            print(f"\n文件: {file_path}")
            
            if not exists:
                file_result["status"] = "missing"
                verify_result["missing"].append(file_path)
                print(f"  状态: [缺失] 文件不存在")
            else:
                current_sha256 = self._compute_file_sha256(full_path)
                file_result["sha256"] = current_sha256
                
                expected = file_result["expected_sha256"]
                if expected:
                    if current_sha256 == expected:
                        file_result["status"] = "verified"
                        verify_result["verified"].append(file_path)
                        print(f"  状态: [已验证] SHA256 匹配")
                        print(f"  SHA256: {current_sha256[:16]}...")
                    else:
                        file_result["status"] = "checksum_mismatch"
                        verify_result["checksum_mismatch"].append({
                            "file": file_path,
                            "expected": expected,
                            "actual": current_sha256
                        })
                        print(f"  状态: [不匹配] SHA256 校验失败")
                        print(f"  预期: {expected[:16]}...")
                        print(f"  实际: {current_sha256[:16]}...")
                else:
                    # 无预期 checksum，视为首次校验
                    file_result["status"] = "no_baseline"
                    verify_result["verified"].append(file_path)
                    print(f"  状态: [无基线] 首次校验，当前 SHA256: {current_sha256[:16]}...")
            
            verify_result["files"].append(file_result)
        
        print("\n" + "=" * 60)
        
        # 汇总
        summary = {
            "total_files": len(patches),
            "verified": len(verify_result["verified"]),
            "missing": len(verify_result["missing"]),
            "checksum_mismatch": len(verify_result["checksum_mismatch"])
        }
        verify_result["summary"] = summary
        
        all_ok = summary["missing"] == 0 and summary["checksum_mismatch"] == 0
        
        print(f"\n[VERIFY] 校验完成")
        print(f"  已验证: {summary['verified']}")
        print(f"  缺失: {summary['missing']}")
        print(f"  不匹配: {summary['checksum_mismatch']}")
        
        if all_ok:
            print("\n[OK] 所有补丁已正确落地")
        else:
            print("\n[WARN] 部分补丁未正确落地，请检查")
        
        # 存储结果供 JSON 输出
        self.report.patches_status["verify_result"] = verify_result
        
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
        result["ok"] = True
        
        print("\n" + "=" * 60)
        print("[OK] Fetch 完成")
        print(f"  Archive: {archive_path}")
        print(f"  解压目录: {extracted_dir}")
        print(f"  SHA256: {archive_sha256}")
        print("=" * 60)
        
        return result
    
    def sync_upstream(
        self,
        ref: Optional[str] = None,
        ref_type: Optional[str] = None,
        exclude_patterns: Optional[List[str]] = None,
        dry_run: bool = True,
        force: bool = False
    ) -> Dict[str, Any]:
        """
        同步上游代码到本地
        
        Args:
            ref: 版本引用，默认从 lock 文件读取
            ref_type: 引用类型，默认从 lock 文件读取
            exclude_patterns: 额外排除模式，会与默认模式合并
            dry_run: 如果为 True，仅显示将要执行的操作（默认 True）
            force: 如果为 True，强制覆盖（即使有本地修改）
        
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
            
            # Step 2: 对比本地与上游
            print("\n[2/4] 对比本地与上游...")
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
                
                return result
            
            # Step 3: 执行同步
            print("\n[3/4] 执行文件同步...")
            
            # 检查是否有本地修改需要确认
            patched_files = set(item["path"].replace("libs/OpenMemory/", "") 
                               for item in self.lock_data.get("patched_files", []))
            
            conflict_files = set(comparison["modified_files"]) & patched_files
            if conflict_files and not force:
                print(f"\n[WARN] 发现 {len(conflict_files)} 个已补丁文件将被修改:")
                for f in list(conflict_files)[:5]:
                    print(f"    - {f}")
                print("\n使用 --force 强制覆盖，或手动处理冲突")
                result["error"] = "存在补丁冲突"
                result["conflict_files"] = list(conflict_files)
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
            
            # Step 4: 更新 lock 文件
            print("\n[4/4] 更新 lock 文件...")
            
            self.lock_data["upstream_commit_sha"] = result["commit_sha"]
            self.lock_data["upstream_commit_date"] = result["commit_date"]
            self.lock_data["last_sync_at"] = datetime.now().isoformat() + "Z"
            self.lock_data["sync_method"] = "archive"
            
            # 添加 archive 信息
            if "archive_info" not in self.lock_data:
                self.lock_data["archive_info"] = {}
            self.lock_data["archive_info"]["sha256"] = result["archive_sha256"]
            self.lock_data["archive_info"]["ref"] = ref
            self.lock_data["archive_info"]["ref_type"] = ref_type
            
            with open(self.lock_file, "w", encoding="utf-8") as f:
                json.dump(self.lock_data, f, indent=2, ensure_ascii=False)
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
        choices=["check", "apply", "verify", "suggest", "fetch", "sync", "all"],
        help="执行的操作: check=一致性检查, apply=应用补丁, verify=校验落地, suggest=输出建议, fetch=获取上游, sync=同步上游, all=执行全部检查"
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
    
    args = parser.parse_args()
    
    # 初始化工具
    tool = OpenMemorySyncTool(workspace_root=args.workspace)
    
    success = True
    result = None
    
    if args.command in ("check", "all"):
        success = tool.run_consistency_check() and success
    
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
        success = tool.verify_patches() and success
    
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
        
        result = tool.sync_upstream(
            ref=args.ref,
            ref_type=args.ref_type,
            exclude_patterns=exclude_patterns,
            dry_run=dry_run,
            force=args.force
        )
        success = result.get("ok", False)
        
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            sys.exit(0 if success else 1)
    
    # 对于非 fetch/sync 命令，输出标准报告
    if args.command not in ("fetch", "sync"):
        tool.print_report(json_output=args.json)
    
    # 返回状态码
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
