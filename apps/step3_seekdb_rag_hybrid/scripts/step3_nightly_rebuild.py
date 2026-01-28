#!/usr/bin/env python3
"""
step3_nightly_rebuild.py - Step3 Nightly Rebuild 标准化流程

实现完整的索引重建 + 门禁验证 + 激活流程：
1. 保存当前 active collection 用于回滚
2. full rebuild 生成带 version_tag 的新 collection（不覆盖旧）
3. 执行 seek_query --query-set 生成聚合门禁
4. 门禁通过后激活新 collection
5. 失败时输出明确的回滚指令

使用:
    # Makefile 入口（推荐）
    make step3-nightly-rebuild
    make step3-nightly-rebuild QUERY_SET=nightly_default
    make step3-nightly-rebuild DRY_RUN=1
    
    # 直接调用
    python scripts/step3_nightly_rebuild.py --query-set nightly_default --json
    python scripts/step3_nightly_rebuild.py --version-tag v2.0.0 --json
    python scripts/step3_nightly_rebuild.py --dry-run --json
    
    # 使用 gate profile
    python scripts/step3_nightly_rebuild.py --gate-profile nightly_default --json
    python scripts/step3_nightly_rebuild.py --gate-profile nightly_default --gate-profile-version 1.0.0 --json

环境变量:
    PROJECT_KEY                    项目标识
    STEP3_PGVECTOR_DSN            PGVector 连接字符串
    STEP3_INDEX_BACKEND           索引后端（默认 pgvector）
    STEP3_GATE_PROFILE            门禁 profile 名称（默认 nightly_default）
    STEP3_GATE_PROFILE_VERSION    门禁 profile 版本（可选，用于审计追踪）
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# 添加父目录到 path 以便导入 gate_profiles
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from gate_profiles import GateProfile, load_gate_profile, get_available_profiles
except ImportError:
    # 如果作为 module 运行
    from step3_seekdb_rag_hybrid.gate_profiles import GateProfile, load_gate_profile, get_available_profiles

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ============ 数据结构 ============


@dataclass
class NightlyRebuildResult:
    """Nightly Rebuild 结果"""
    success: bool = False
    
    # 阶段状态
    phase: str = "init"  # init/get_active/rebuild/gate/activate/done/failed
    
    # Collection 信息
    old_collection: Optional[str] = None
    new_collection: Optional[str] = None
    version_tag: Optional[str] = None
    activated: bool = False
    
    # 重建结果
    rebuild_success: bool = False
    rebuild_total_indexed: int = 0
    rebuild_total_errors: int = 0
    rebuild_duration_seconds: float = 0.0
    
    # 门禁结果
    gate_passed: bool = False
    gate_total_queries: int = 0
    gate_pass_count: int = 0
    gate_warn_count: int = 0
    gate_fail_count: int = 0
    gate_error_count: int = 0
    gate_worst_recommendation: str = "unknown"
    
    # Gate Profile 信息
    gate_profile: Optional[Dict[str, Any]] = None
    
    # 时间信息
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: float = 0.0
    
    # 错误信息
    error: Optional[str] = None
    error_phase: Optional[str] = None
    
    # 回滚指令
    rollback_command: Optional[str] = None
    
    # 激活被阻止信息
    activation_blocked: bool = False
    activation_blocked_info: Optional[Dict[str, Any]] = None
    how_to_enable: Optional[str] = None
    how_to_manual_activate: Optional[str] = None
    how_to_manual_rollback: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "phase": self.phase,
            "collection": {
                "old": self.old_collection,
                "new": self.new_collection,
                "version_tag": self.version_tag,
                "activated": self.activated,
            },
            "rebuild": {
                "success": self.rebuild_success,
                "total_indexed": self.rebuild_total_indexed,
                "total_errors": self.rebuild_total_errors,
                "duration_seconds": self.rebuild_duration_seconds,
            },
            "gate": {
                "passed": self.gate_passed,
                "total_queries": self.gate_total_queries,
                "pass_count": self.gate_pass_count,
                "warn_count": self.gate_warn_count,
                "fail_count": self.gate_fail_count,
                "error_count": self.gate_error_count,
                "worst_recommendation": self.gate_worst_recommendation,
            },
            "gate_profile": self.gate_profile,
            "timing": {
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "duration_seconds": self.duration_seconds,
            },
            "error": {
                "message": self.error,
                "phase": self.error_phase,
            } if self.error else None,
            "rollback": {
                "command": self.rollback_command,
            } if self.rollback_command else None,
            "activation_blocked": {
                "blocked": self.activation_blocked,
                "info": self.activation_blocked_info,
                "how_to_enable": self.how_to_enable,
                "how_to_manual_activate": self.how_to_manual_activate,
                "how_to_manual_rollback": self.how_to_manual_rollback,
            } if self.activation_blocked else None,
        }


# ============ 辅助函数 ============


def run_command(
    cmd: List[str],
    capture_output: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    """执行命令并返回结果"""
    logger.debug(f"执行命令: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # apps/step3_seekdb_rag_hybrid
        )
        return result
    except subprocess.TimeoutExpired as e:
        logger.error(f"命令超时: {' '.join(cmd)}")
        raise


def parse_json_output(output: str) -> Optional[Dict[str, Any]]:
    """解析 JSON 输出，处理多行输出情况"""
    if not output:
        return None
    
    # 尝试解析整个输出
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass
    
    # 尝试按行解析，找到最后一个有效的 JSON 对象
    for line in reversed(output.strip().split('\n')):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    
    return None


# ============ 主流程 ============


def get_active_collection(project_key: Optional[str] = None) -> Optional[str]:
    """获取当前 active collection"""
    cmd = [
        sys.executable, "-m", "seek_indexer",
        "--mode", "show-active",
        "--json",
    ]
    if project_key:
        cmd.extend(["--project-key", project_key])
    
    result = run_command(cmd)
    if result.returncode != 0:
        logger.warning(f"获取 active collection 失败: {result.stderr}")
        return None
    
    data = parse_json_output(result.stdout)
    if data and data.get("success"):
        return data.get("active_collection")
    
    return None


def run_full_rebuild(
    version_tag: Optional[str] = None,
    project_key: Optional[str] = None,
    source: str = "all",
    batch_size: int = 100,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """执行全量重建"""
    cmd = [
        sys.executable, "-m", "seek_indexer",
        "--mode", "full",
        "--source", source,
        "--batch-size", str(batch_size),
        "--json",
    ]
    
    if version_tag:
        cmd.extend(["--version-tag", version_tag])
    if project_key:
        cmd.extend(["--project-key", project_key])
    if dry_run:
        cmd.append("--dry-run")
    
    # 不使用 --activate，我们要在 gate 通过后再激活
    
    result = run_command(cmd, timeout=1800)  # 30 分钟超时
    
    data = parse_json_output(result.stdout)
    if data is None:
        return {
            "success": False,
            "error": f"无法解析输出: {result.stdout[:500]}...",
            "stderr": result.stderr,
        }
    
    return data


def run_gate_check(
    query_set: str = "nightly_default",
    project_key: Optional[str] = None,
    min_overlap: float = 0.5,
    top_k: int = 10,
    gate_profile: Optional[GateProfile] = None,
) -> Dict[str, Any]:
    """执行门禁检查
    
    Args:
        query_set: 查询集名称
        project_key: 项目标识
        min_overlap: 最小重叠率阈值
        top_k: 返回结果数量
        gate_profile: Gate Profile 配置，如果提供则使用其阈值覆盖 min_overlap/top_k
    
    Returns:
        门禁检查结果字典
    """
    # 如果提供了 gate_profile，使用其配置
    if gate_profile is not None:
        min_overlap = gate_profile.min_overlap
        top_k = gate_profile.top_k
        query_set = gate_profile.query_set
        logger.info(
            f"使用 GateProfile: name={gate_profile.name}, version={gate_profile.version}, "
            f"source={gate_profile.source}, min_overlap={min_overlap}, top_k={top_k}"
        )
    
    cmd = [
        sys.executable, "-m", "seek_query",
        "--query-set", query_set,
        "--dual-read",
        "--dual-read-min-overlap", str(min_overlap),
        "--dual-read-report",
        "--top-k", str(top_k),
        "--json",
    ]
    
    if project_key:
        cmd.extend(["--project-key", project_key])
    
    result = run_command(cmd, timeout=600)  # 10 分钟超时
    
    data = parse_json_output(result.stdout)
    if data is None:
        # 如果无法解析 JSON，检查退出码
        if result.returncode == 0:
            return {
                "success": True,
                "aggregate_gate": {"passed": True},
            }
        return {
            "success": False,
            "error": f"无法解析输出: {result.stdout[:500]}...",
            "stderr": result.stderr,
        }
    
    return data


@dataclass
class ActivateResult:
    """激活结果"""
    success: bool = False
    blocked: bool = False
    blocked_info: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def activate_collection(
    collection_id: str,
    project_key: Optional[str] = None,
) -> ActivateResult:
    """激活 collection
    
    Returns:
        ActivateResult: 包含激活结果、是否被阻止等信息
    """
    result = ActivateResult()
    
    cmd = [
        sys.executable, "-m", "seek_indexer",
        "--mode", "validate-switch",
        "--collection", collection_id,
        "--activate",
        "--json",
    ]
    
    if project_key:
        cmd.extend(["--project-key", project_key])
    
    cmd_result = run_command(cmd)
    
    # 尝试解析输出
    data = parse_json_output(cmd_result.stdout)
    
    if cmd_result.returncode == 0 and data and data.get("success"):
        result.success = True
        return result
    
    # 检查是否被阻止
    if data:
        activation = data.get("activation", {})
        if activation.get("blocked"):
            result.blocked = True
            result.blocked_info = activation.get("blocked_info")
            result.error = "激活被阻止: STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH 未启用"
            return result
    
    # 其他失败原因
    result.error = cmd_result.stderr or "激活 collection 失败"
    logger.error(f"激活 collection 失败: {result.error}")
    return result


def run_nightly_rebuild(
    query_set: str = "nightly_default",
    version_tag: Optional[str] = None,
    project_key: Optional[str] = None,
    source: str = "all",
    batch_size: int = 100,
    min_overlap: float = 0.5,
    top_k: int = 10,
    dry_run: bool = False,
    skip_gate: bool = False,
    gate_profile_name: Optional[str] = None,
    gate_profile_version: Optional[str] = None,
) -> NightlyRebuildResult:
    """
    执行完整的 Nightly Rebuild 流程
    
    流程:
    1. 获取当前 active collection（用于回滚）
    2. 执行 full rebuild 生成新 collection
    3. 执行门禁检查（使用 Gate Profile 配置）
    4. 门禁通过后激活新 collection
    5. 失败时生成回滚指令
    
    Args:
        query_set: 查询集名称（可被 gate_profile 覆盖）
        version_tag: 版本标签
        project_key: 项目标识
        source: 数据源
        batch_size: 批量大小
        min_overlap: 最小重叠率阈值（可被 gate_profile 覆盖）
        top_k: 返回结果数量（可被 gate_profile 覆盖）
        dry_run: 是否为 dry-run 模式
        skip_gate: 是否跳过门禁检查
        gate_profile_name: Gate Profile 名称
        gate_profile_version: Gate Profile 版本（用于审计追踪）
    """
    result = NightlyRebuildResult()
    result.started_at = datetime.now(timezone.utc).isoformat()
    
    start_time = datetime.now(timezone.utc)
    
    # 加载 Gate Profile（如果指定）
    gate_profile: Optional[GateProfile] = None
    if gate_profile_name is not None:
        try:
            # 构建 CLI 覆盖参数（只有当参数不是默认值时才覆盖）
            overrides: Dict[str, Any] = {}
            if min_overlap != 0.5:
                overrides["min_overlap"] = min_overlap
            if top_k != 10:
                overrides["top_k"] = top_k
            if query_set != "nightly_default":
                overrides["query_set"] = query_set
            
            gate_profile = load_gate_profile(gate_profile_name, overrides=overrides if overrides else None)
            
            # 如果指定了版本，覆盖自动生成的版本
            if gate_profile_version:
                gate_profile.version = gate_profile_version
            
            # 将 gate_profile 信息保存到结果中
            result.gate_profile = gate_profile.to_dict()
            
            logger.info(
                f"已加载 GateProfile: name={gate_profile.name}, "
                f"version={gate_profile.version}, source={gate_profile.source}"
            )
        except ValueError as e:
            logger.error(f"加载 GateProfile 失败: {e}")
            result.error = str(e)
            result.error_phase = "init"
            result.phase = "failed"
            return result
    
    try:
        # ========== 阶段 1: 获取当前 active collection ==========
        result.phase = "get_active"
        logger.info("阶段 1: 获取当前 active collection...")
        
        old_collection = get_active_collection(project_key)
        result.old_collection = old_collection
        
        if old_collection:
            logger.info(f"当前 active collection: {old_collection}")
        else:
            logger.info("当前无 active collection")
        
        # ========== 阶段 2: 执行 full rebuild ==========
        result.phase = "rebuild"
        logger.info("阶段 2: 执行 full rebuild...")
        
        rebuild_result = run_full_rebuild(
            version_tag=version_tag,
            project_key=project_key,
            source=source,
            batch_size=batch_size,
            dry_run=dry_run,
        )
        
        if not rebuild_result.get("success", False):
            result.error = rebuild_result.get("error", "重建失败")
            result.error_phase = "rebuild"
            raise Exception(result.error)
        
        result.rebuild_success = True
        result.new_collection = rebuild_result.get("collection", {}).get("name")
        result.version_tag = rebuild_result.get("collection", {}).get("version_tag")
        result.rebuild_total_indexed = rebuild_result.get("total_indexed", 0)
        result.rebuild_total_errors = rebuild_result.get("total_errors", 0)
        result.rebuild_duration_seconds = rebuild_result.get("duration_seconds", 0.0)
        
        logger.info(f"重建完成: collection={result.new_collection}, indexed={result.rebuild_total_indexed}")
        
        # 如果是 dry-run 模式，跳过后续步骤
        if dry_run:
            result.phase = "done"
            result.success = True
            logger.info("Dry-run 模式，跳过门禁和激活")
            return result
        
        # ========== 阶段 3: 执行门禁检查 ==========
        if skip_gate:
            result.phase = "activate"
            result.gate_passed = True
            logger.info("跳过门禁检查（--skip-gate）")
        else:
            result.phase = "gate"
            # 确定实际使用的 query_set（gate_profile 优先）
            effective_query_set = gate_profile.query_set if gate_profile else query_set
            logger.info(f"阶段 3: 执行门禁检查 (query_set={effective_query_set})...")
            
            gate_result = run_gate_check(
                query_set=query_set,
                project_key=project_key,
                min_overlap=min_overlap,
                top_k=top_k,
                gate_profile=gate_profile,
            )
            
            # 解析门禁结果
            aggregate_gate = gate_result.get("aggregate_gate", {})
            result.gate_passed = aggregate_gate.get("passed", False)
            result.gate_total_queries = aggregate_gate.get("total_queries", 0)
            result.gate_pass_count = aggregate_gate.get("pass_count", 0)
            result.gate_warn_count = aggregate_gate.get("warn_count", 0)
            result.gate_fail_count = aggregate_gate.get("fail_count", 0)
            result.gate_error_count = aggregate_gate.get("error_count", 0)
            result.gate_worst_recommendation = aggregate_gate.get("worst_recommendation", "unknown")
            
            if not result.gate_passed:
                result.error = f"门禁失败: fail={result.gate_fail_count}, worst={result.gate_worst_recommendation}"
                result.error_phase = "gate"
                raise Exception(result.error)
            
            logger.info(f"门禁通过: pass={result.gate_pass_count}, warn={result.gate_warn_count}")
        
        # ========== 阶段 4: 激活新 collection ==========
        result.phase = "activate"
        logger.info(f"阶段 4: 激活新 collection: {result.new_collection}...")
        
        activate_result = activate_collection(result.new_collection, project_key)
        
        if activate_result.success:
            result.activated = True
            logger.info("激活成功")
        elif activate_result.blocked:
            # 激活被 STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH 阻止
            result.activation_blocked = True
            result.activation_blocked_info = activate_result.blocked_info
            result.error = activate_result.error
            result.error_phase = "activate"
            
            # 生成启用和手动操作指令
            result.how_to_enable = "设置环境变量 STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH=1 或 STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH=true"
            result.how_to_manual_activate = f"STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH=1 python -m seek_indexer --mode validate-switch --collection \"{result.new_collection}\" --activate"
            if project_key:
                result.how_to_manual_activate += f" --project-key {project_key}"
            if result.old_collection:
                result.how_to_manual_rollback = f"STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH=1 python -m seek_indexer --mode rollback --collection \"{result.old_collection}\""
                if project_key:
                    result.how_to_manual_rollback += f" --project-key {project_key}"
            
            logger.error(f"激活被阻止: STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH 未启用")
            logger.error(f"启用方式: {result.how_to_enable}")
            logger.error(f"手动激活命令: {result.how_to_manual_activate}")
            if result.how_to_manual_rollback:
                logger.error(f"手动回滚命令: {result.how_to_manual_rollback}")
            logger.error(f"查看当前 active collection: python -m seek_indexer --mode show-active --json")
            
            raise Exception(result.error)
        else:
            result.error = activate_result.error or "激活失败"
            result.error_phase = "activate"
            raise Exception(result.error)
        
        # ========== 完成 ==========
        result.phase = "done"
        result.success = True
        logger.info("Nightly Rebuild 完成")
        
    except Exception as e:
        result.success = False
        result.phase = "failed"
        if not result.error:
            result.error = str(e)
            result.error_phase = result.phase
        
        # 生成回滚指令
        if result.old_collection:
            rollback_cmd = f"python -m seek_indexer --mode rollback --collection \"{result.old_collection}\""
            if project_key:
                rollback_cmd += f" --project-key {project_key}"
            result.rollback_command = rollback_cmd
            logger.error(f"回滚指令: {rollback_cmd}")
        
        logger.error(f"Nightly Rebuild 失败: {result.error}")
    
    finally:
        end_time = datetime.now(timezone.utc)
        result.completed_at = end_time.isoformat()
        result.duration_seconds = (end_time - start_time).total_seconds()
    
    return result


# ============ CLI ============


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Step3 Nightly Rebuild 标准化流程",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 使用默认配置执行 nightly rebuild
    python step3_nightly_rebuild.py --json
    
    # 指定查询集
    python step3_nightly_rebuild.py --query-set nightly_default --json
    
    # 指定版本标签
    python step3_nightly_rebuild.py --version-tag v2.0.0 --json
    
    # Dry-run 模式（仅重建，不激活）
    python step3_nightly_rebuild.py --dry-run --json
    
    # 跳过门禁检查
    python step3_nightly_rebuild.py --skip-gate --json
    
    # 使用 Gate Profile（推荐）
    python step3_nightly_rebuild.py --gate-profile nightly_default --json
    
    # 指定 Profile 版本（用于审计追踪）
    python step3_nightly_rebuild.py --gate-profile nightly_default --gate-profile-version 1.0.0 --json

环境变量:
    PROJECT_KEY                    项目标识
    STEP3_PGVECTOR_DSN            PGVector 连接字符串
    STEP3_GATE_PROFILE            门禁 Profile 名称（默认 nightly_default）
    STEP3_GATE_PROFILE_VERSION    门禁 Profile 版本
        """,
    )
    
    parser.add_argument(
        "--query-set",
        type=str,
        default=os.environ.get("STEP3_NIGHTLY_QUERY_SET", "nightly_default"),
        help="门禁使用的查询集（默认 nightly_default）",
    )
    parser.add_argument(
        "--version-tag",
        type=str,
        default=None,
        help="版本标签（默认自动生成时间戳）",
    )
    parser.add_argument(
        "--project-key",
        type=str,
        default=os.environ.get("PROJECT_KEY"),
        help="项目标识",
    )
    parser.add_argument(
        "--source",
        type=str,
        choices=["patch_blobs", "attachments", "all"],
        default=os.environ.get("INDEX_SOURCE", "all"),
        help="数据源（默认 all）",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("BATCH_SIZE", "100")),
        help="批量大小（默认 100）",
    )
    parser.add_argument(
        "--min-overlap",
        type=float,
        default=float(os.environ.get("STEP3_NIGHTLY_MIN_OVERLAP", "0.5")),
        help="门禁最小 overlap 阈值（默认 0.5）",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=int(os.environ.get("STEP3_NIGHTLY_TOP_K", "10")),
        help="门禁查询返回数量（默认 10）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
        help="Dry-run 模式（仅重建，跳过门禁和激活）",
    )
    parser.add_argument(
        "--skip-gate",
        action="store_true",
        default=os.environ.get("STEP3_NIGHTLY_SKIP_GATE", "").lower() in ("1", "true", "yes"),
        help="跳过门禁检查（直接激活）",
    )
    parser.add_argument(
        "--gate-profile",
        type=str,
        default=os.environ.get("STEP3_GATE_PROFILE"),
        choices=get_available_profiles(),
        help=f"门禁 Profile 名称，可选: {', '.join(get_available_profiles())}（从环境变量 STEP3_GATE_PROFILE 读取）",
    )
    parser.add_argument(
        "--gate-profile-version",
        type=str,
        default=os.environ.get("STEP3_GATE_PROFILE_VERSION"),
        help="门禁 Profile 版本（用于审计追踪，从环境变量 STEP3_GATE_PROFILE_VERSION 读取）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )
    
    return parser.parse_args()


def print_report(result: NightlyRebuildResult):
    """打印报告（文本格式）"""
    print("\n" + "=" * 60)
    print("Step3 Nightly Rebuild 报告")
    print("=" * 60)
    
    print(f"\n【状态】")
    print(f"  结果: {'成功' if result.success else '失败'}")
    print(f"  阶段: {result.phase}")
    print(f"  耗时: {result.duration_seconds:.2f} 秒")
    
    print(f"\n【Collection】")
    print(f"  旧 Collection: {result.old_collection or '无'}")
    print(f"  新 Collection: {result.new_collection or '未创建'}")
    if result.version_tag:
        print(f"  版本标签: {result.version_tag}")
    print(f"  已激活: {'是' if result.activated else '否'}")
    
    if result.rebuild_success:
        print(f"\n【重建统计】")
        print(f"  索引成功: {result.rebuild_total_indexed}")
        print(f"  错误数: {result.rebuild_total_errors}")
        print(f"  耗时: {result.rebuild_duration_seconds:.2f} 秒")
    
    if result.gate_profile:
        print(f"\n【门禁 Profile】")
        print(f"  名称: {result.gate_profile.get('name', 'unknown')}")
        print(f"  版本: {result.gate_profile.get('version', 'unknown')}")
        print(f"  来源: {result.gate_profile.get('source', 'unknown')}")
        thresholds = result.gate_profile.get('thresholds', {})
        if thresholds:
            print(f"  阈值: min_overlap={thresholds.get('min_overlap')}, top_k={thresholds.get('top_k')}")
    
    if result.gate_total_queries > 0:
        print(f"\n【门禁结果】")
        print(f"  通过: {'是' if result.gate_passed else '否'}")
        print(f"  查询数: {result.gate_total_queries}")
        print(f"  Pass: {result.gate_pass_count}")
        print(f"  Warn: {result.gate_warn_count}")
        print(f"  Fail: {result.gate_fail_count}")
        print(f"  Error: {result.gate_error_count}")
        print(f"  最差建议: {result.gate_worst_recommendation}")
    
    if result.error:
        print(f"\n【错误】")
        print(f"  消息: {result.error}")
        print(f"  阶段: {result.error_phase}")
    
    if result.rollback_command:
        print(f"\n【回滚指令】")
        print(f"  {result.rollback_command}")
    
    print("\n" + "=" * 60)
    print("Nightly Rebuild " + ("成功" if result.success else "失败"))
    print("=" * 60 + "\n")


def main() -> int:
    """主入口"""
    args = parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.json:
        logging.getLogger().setLevel(logging.WARNING)
    
    # 执行 nightly rebuild
    result = run_nightly_rebuild(
        query_set=args.query_set,
        version_tag=args.version_tag,
        project_key=args.project_key,
        source=args.source,
        batch_size=args.batch_size,
        min_overlap=args.min_overlap,
        top_k=args.top_k,
        dry_run=args.dry_run,
        skip_gate=args.skip_gate,
        gate_profile_name=args.gate_profile,
        gate_profile_version=args.gate_profile_version,
    )
    
    # 输出结果
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print_report(result)
    
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
