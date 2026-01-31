#!/usr/bin/env python3
"""
环境变量一致性检查脚本

检查 `.env.example`、`docs/reference/environment_variables.md` 和
`src/engram/**/config.py` 中的环境变量集合是否一致。

用法:
    python scripts/ci/check_env_var_consistency.py [--strict] [--json] [--verbose]

选项:
    --strict    严格模式，任何差异都会导致失败
    --json      JSON 格式输出
    --verbose   显示详细信息

退出码:
    0 - 检查通过
    1 - 存在不一致（缺失或多余的变量）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set


# ============================================================================
# 配置区
# ============================================================================

# 允许只在 .env.example 中存在的变量（Docker/部署相关，非代码读取）
ENV_EXAMPLE_ONLY_VARS: Set[str] = {
    # Docker Compose / 镜像配置
    "POSTGRES_IMAGE",
    "OPENMEMORY_IMAGE",
    # PostgreSQL 超级用户配置（容器初始化用）
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
    # 服务账号密码（容器环境变量注入，代码不直接读取）
    "LOGBOOK_MIGRATOR_PASSWORD",
    "LOGBOOK_SVC_PASSWORD",
    "OPENMEMORY_MIGRATOR_PASSWORD",
    "OPENMEMORY_SVC_PASSWORD",
    # MinIO 配置（可选组件）
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "MINIO_BUCKET",
    "MINIO_API_PORT",
    "MINIO_CONSOLE_PORT",
    # Dashboard 配置（可选组件）
    "METABASE_PORT",
    "MB_ENCRYPTION_SECRET_KEY",
    "PGADMIN_PORT",
    "PGADMIN_DEFAULT_EMAIL",
    "PGADMIN_DEFAULT_PASSWORD",
    # OpenMemory 上游组件配置
    "OM_PORT",
    "OM_API_KEY",
    "OM_PG_SCHEMA",
    "OM_METADATA_BACKEND",
    "OM_VECTOR_BACKEND",
    "OM_PG_AUTO_DDL",
    # Gateway 运行时配置（文档中 Gateway 组件章节已描述，表格可能未列出）
    "GATEWAY_LOG_LEVEL",
    "GATEWAY_PORT",
    # SCM 组件运行时配置（docker-compose / 独立脚本使用，不在 config.py 中读取）
    "SCM_REAPER_INTERVAL_SECONDS",
    "SCM_REAPER_JOB_GRACE_SECONDS",
    "SCM_REAPER_LOCK_GRACE_SECONDS",
    "SCM_SCHEDULER_MAX_RUNNING",
    "SCM_SCHEDULER_GLOBAL_CONCURRENCY",
    "SCM_SCHEDULER_SCAN_INTERVAL_SECONDS",
    "SCM_WORKER_LEASE_SECONDS",
    "SCM_WORKER_POLL_INTERVAL",
    "SCM_WORKER_PARALLELISM",
}

# 允许只在文档中存在的变量（高级配置/上游组件/历史兼容）
DOC_ONLY_VARS: Set[str] = {
    # CI 门禁配置（脚本专用，不在 .env.example 中设置，由 CI 环境变量显式传递）
    "ENGRAM_MYPY_GATE",
    "ENGRAM_MYPY_BASELINE_FILE",
    "ENGRAM_MYPY_PATH",
    "MYPY_GATE",  # [已废弃] 兼容别名，将在未来版本移除
    "ENGRAM_VERIFY_GATE",
    "ENGRAM_VERIFY_STRICT",
    # OpenMemory 上游组件详细配置（不在 engram 代码中读取）
    "OM_MODE",
    "OM_TIER",
    "OM_DB_PATH",
    "OM_PG_HOST",
    "OM_PG_PORT",
    "OM_PG_DB",
    "OM_PG_USER",
    "OM_PG_PASSWORD",
    "OM_PG_TABLE",
    "OM_PG_SSL",
    "OM_PG_AUTO_CREATE_DB",
    "OM_PG_SET_ROLE",
    "OM_VECTOR_TABLE",
    "OM_VEC_DIM",
    "OM_EMBEDDINGS",
    "OM_EMBEDDING_FALLBACK",
    "OM_EMBED_MODE",
    "OPENAI_API_KEY",
    "OM_OPENAI_BASE_URL",
    "OM_OPENAI_MODEL",
    "GEMINI_API_KEY",
    "OLLAMA_URL",
    "OM_MIN_SCORE",
    "OM_MAX_PAYLOAD_SIZE",
    "OM_USE_SUMMARY_ONLY",
    "OM_SUMMARY_MAX_LENGTH",
    "OM_SEG_SIZE",
    "OM_DECAY_INTERVAL_MINUTES",
    "OM_DECAY_THREADS",
    "OM_DECAY_COLD_THRESHOLD",
    "OM_DECAY_REINFORCE_ON_QUERY",
    "OM_REGENERATION_ENABLED",
    "OM_AUTO_REFLECT",
    "OM_REFLECT_INTERVAL",
    "OM_REFLECT_MIN_MEMORIES",
    "OM_RATE_LIMIT_ENABLED",
    "OM_RATE_LIMIT_WINDOW_MS",
    "OM_RATE_LIMIT_MAX_REQUESTS",
    # Logbook 配置文件相关
    "ENGRAM_LOGBOOK_CONFIG",
    "POSTGRES_DSN",
    # MinIO 高级配置
    "MINIO_APP_USER",
    "MINIO_APP_PASSWORD",
    "MINIO_ALLOWED_PREFIXES",
    "MINIO_CREATE_OPS_USER",
    "MINIO_OPS_USER",
    "MINIO_OPS_PASSWORD",
    "MINIO_FORCE_HTTPS",
    "MINIO_CERTS_DIR",
    "MINIO_AUDIT_WEBHOOK_ENDPOINT",
    "MINIO_AUDIT_CLIENT_CERT",
    "MINIO_AUDIT_CLIENT_KEY",
    "MINIO_AUDIT_QUEUE_DIR",
    "MINIO_AUDIT_QUEUE_SIZE",
    "MINIO_ENABLE_VERSIONING",
    # Dashboard 配置
    "DASHBOARD_PORT",
    "NEXT_PUBLIC_API_URL",
    "NEXT_PUBLIC_API_KEY",
    # SCM 凭证配置（敏感，不在 .env.example 中提供默认值）
    "GITLAB_PRIVATE_TOKEN",
    "GITLAB_TOKEN",
    "GITLAB_URL",
    "SVN_USERNAME",
    "SVN_PASSWORD",
    # Vendoring 配置
    "OPENMEMORY_PATCH_FILES_REQUIRED",
    "SCHEMA_STRICT",
    "UPSTREAM_REF",
    "DRY_RUN",
    # S3 配置
    "ENGRAM_S3_ENDPOINT",
    "ENGRAM_S3_BUCKET",
    "ENGRAM_S3_REGION",
    "ENGRAM_S3_USE_OPS",
    "ENGRAM_S3_VERIFY_SSL",
    # Artifacts S3 凭证（敏感信息，不在 .env.example 中）
    "ENGRAM_S3_ACCESS_KEY",
    "ENGRAM_S3_SECRET_KEY",
    # Gateway 高级配置（有合理默认值，不需要在 .env.example 中设置）
    "DEFAULT_TEAM_SPACE",
    "PRIVATE_SPACE_PREFIX",
    "OPENMEMORY_API_KEY",
    "OPENMEMORY_BASE_URL",
    "MINIO_AUDIT_WEBHOOK_AUTH_TOKEN",
    "MINIO_AUDIT_MAX_PAYLOAD_SIZE",
    # Gateway 启动与治理配置（有合理默认值）
    "AUTO_MIGRATE_ON_STARTUP",
    "LOGBOOK_CHECK_ON_STARTUP",
    "GOVERNANCE_ADMIN_KEY",
    "UNKNOWN_ACTOR_POLICY",
    # Gateway Evidence Refs 校验配置（有合理默认值）
    "VALIDATE_EVIDENCE_REFS",
    "STRICT_MODE_ENFORCE_VALIDATE_REFS",
    # SCM Claim 配置（有合理默认值）
    "SCM_CLAIM_ENABLE_TENANT_FAIR_CLAIM",
    "SCM_CLAIM_MAX_CONSECUTIVE_SAME_TENANT",
    "SCM_CLAIM_MAX_TENANTS_PER_ROUND",
    # Outbox Worker 配置（高级配置，有合理默认值）
    "WORKER_POLL_INTERVAL",
    "WORKER_BATCH_SIZE",
    "WORKER_LEASE_TIMEOUT",
    # SCM 熔断器配置（高级配置，有合理默认值）
    "SCM_CB_BACKFILL_INTERVAL_SECONDS",
    "SCM_CB_BACKFILL_ONLY_MODE",
    "SCM_CB_DEGRADED_BATCH_SIZE",
    "SCM_CB_DEGRADED_FORWARD_WINDOW_SECONDS",
    "SCM_CB_ENABLE_SMOOTHING",
    "SCM_CB_FAILURE_RATE_THRESHOLD",
    "SCM_CB_HALF_OPEN_MAX_REQUESTS",
    "SCM_CB_MIN_SAMPLES",
    "SCM_CB_OPEN_DURATION_SECONDS",
    "SCM_CB_PROBE_BUDGET_PER_INTERVAL",
    "SCM_CB_PROBE_JOB_TYPES_ALLOWLIST",
    "SCM_CB_RATE_LIMIT_THRESHOLD",
    "SCM_CB_RECOVERY_SUCCESS_COUNT",
    "SCM_CB_SMOOTHING_ALPHA",
    "SCM_CB_TIMEOUT_RATE_THRESHOLD",
    "SCM_CB_WINDOW_COUNT",
    "SCM_CB_WINDOW_MINUTES",
    # SCM Scheduler 高级配置（环境变量覆盖，有合理默认值）
    "SCM_SCHEDULER_BACKFILL_REPAIR_WINDOW_HOURS",
    "SCM_SCHEDULER_CURSOR_AGE_THRESHOLD_SECONDS",
    "SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS",
    "SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD",
    "SCM_SCHEDULER_LOG_LEVEL",
    "SCM_SCHEDULER_MAX_BACKFILL_WINDOW_HOURS",
    "SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN",
    "SCM_SCHEDULER_MVP_JOB_TYPE_ALLOWLIST",
    "SCM_SCHEDULER_MVP_MODE_ENABLED",
    "SCM_SCHEDULER_PAUSE_DURATION_SECONDS",
    "SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY",
    "SCM_SCHEDULER_PER_TENANT_CONCURRENCY",
    "SCM_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND",
    # SCM Worker 高级配置（环境变量覆盖，有合理默认值）
    "SCM_WORKER_BATCH_SIZE",
    "SCM_WORKER_LOCK_TIMEOUT",
    "SCM_WORKER_LOG_LEVEL",
    "SCM_WORKER_MAX_RENEW_FAILURES",
    "SCM_WORKER_RENEW_INTERVAL_SECONDS",
    # SCM Reaper 高级配置（环境变量覆盖，有合理默认值）
    "SCM_REAPER_LOG_LEVEL",
    "SCM_REAPER_RUN_MAX_SECONDS",
}

# 允许只在代码中存在的变量（内部配置/测试用/兼容别名）
CODE_ONLY_VARS: Set[str] = {
    # 测试相关
    "ENGRAM_TESTING",
    "TEST_PG_DSN",
    "TEST_PG_ADMIN_DSN",
    # 内部开关
    "ENGRAM_SCM_SYNC_ENABLED",
    # 向后兼容别名（POSTGRES_DSN 的别名）
    "DATABASE_URL",
    # GC 治理配置（功能内部使用，文档待补充）
    "ENGRAM_GC_REQUIRE_OPS_DEFAULT",
    "ENGRAM_GC_REQUIRE_TRASH_DEFAULT",
    # Artifacts 配置（logbook/config.py 内部使用，文档待补充）
    "ENGRAM_ARTIFACTS_ROOT",
    "ENGRAM_ARTIFACTS_BACKEND",
    "ENGRAM_ARTIFACTS_READ_ONLY",
    # 管理员 DSN（logbook/config.py 内部使用）
    "ENGRAM_PG_ADMIN_DSN",
}


# ============================================================================
# 解析函数
# ============================================================================


def parse_env_example(file_path: Path) -> Set[str]:
    """从 .env.example 解析环境变量名"""
    vars_set: Set[str] = set()

    if not file_path.exists():
        return vars_set

    content = file_path.read_text(encoding="utf-8")

    for line in content.splitlines():
        line = line.strip()
        # 跳过空行和注释
        if not line or line.startswith("#"):
            continue
        # 解析 VAR=value 形式
        if "=" in line:
            var_name = line.split("=", 1)[0].strip()
            if var_name and var_name[0].isupper():
                vars_set.add(var_name)

    return vars_set


def parse_env_doc(file_path: Path) -> Set[str]:
    """从环境变量文档解析环境变量名"""
    vars_set: Set[str] = set()

    if not file_path.exists():
        return vars_set

    content = file_path.read_text(encoding="utf-8")

    # 匹配 Markdown 表格中的变量名：| `VAR_NAME` | 或 | VAR_NAME |
    # 表格行格式：| 变量 | 说明 | 默认值 | 必填 |
    table_pattern = re.compile(r"^\|\s*`?([A-Z][A-Z0-9_]+)`?\s*\|", re.MULTILINE)
    for match in table_pattern.finditer(content):
        var_name = match.group(1).strip("`")
        vars_set.add(var_name)

    return vars_set


def parse_config_py_files(config_dir: Path) -> Set[str]:
    """从 config.py 文件解析环境变量名"""
    vars_set: Set[str] = set()

    # 查找所有 config.py 文件
    config_files = list(config_dir.rglob("config.py"))

    for config_file in config_files:
        content = config_file.read_text(encoding="utf-8")

        # 匹配 os.environ.get("VAR") 或 os.environ["VAR"] 或 os.getenv("VAR")
        patterns = [
            r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]+)["\']',
            r'os\.environ\[\s*["\']([A-Z][A-Z0-9_]+)["\']',
            r'os\.getenv\(\s*["\']([A-Z][A-Z0-9_]+)["\']',
            # 匹配常量定义：ENV_XXX = "VAR_NAME"
            r'ENV_[A-Z_]+\s*=\s*["\']([A-Z][A-Z0-9_]+)["\']',
            # 匹配辅助函数调用：_get_optional_env("VAR"), _get_required_env("VAR")
            r'_get_(?:optional|required)_env\(\s*["\']([A-Z][A-Z0-9_]+)["\']',
            # 匹配带环境变量名的函数调用（通用模式）
            r'_get_env_or_config\(\s*["\']([A-Z][A-Z0-9_]+)["\']',
            r'_get_env_or_config_bool\(\s*["\']([A-Z][A-Z0-9_]+)["\']',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, content):
                vars_set.add(match.group(1))

    return vars_set


# ============================================================================
# 检查逻辑
# ============================================================================


@dataclass
class CheckResult:
    """检查结果"""

    env_example_vars: Set[str] = field(default_factory=set)
    doc_vars: Set[str] = field(default_factory=set)
    code_vars: Set[str] = field(default_factory=set)

    # 差异分析
    in_example_not_in_doc: Set[str] = field(default_factory=set)
    in_doc_not_in_example: Set[str] = field(default_factory=set)
    in_example_not_in_code: Set[str] = field(default_factory=set)
    in_code_not_in_example: Set[str] = field(default_factory=set)
    in_doc_not_in_code: Set[str] = field(default_factory=set)
    in_code_not_in_doc: Set[str] = field(default_factory=set)

    # 过滤后的问题（排除允许列表）
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


def analyze_consistency(
    env_example_vars: Set[str],
    doc_vars: Set[str],
    code_vars: Set[str],
) -> CheckResult:
    """分析三个来源的一致性"""
    result = CheckResult(
        env_example_vars=env_example_vars,
        doc_vars=doc_vars,
        code_vars=code_vars,
    )

    # 计算差异
    result.in_example_not_in_doc = env_example_vars - doc_vars
    result.in_doc_not_in_example = doc_vars - env_example_vars
    result.in_example_not_in_code = env_example_vars - code_vars
    result.in_code_not_in_example = code_vars - env_example_vars
    result.in_doc_not_in_code = doc_vars - code_vars
    result.in_code_not_in_doc = code_vars - doc_vars

    # 过滤掉允许列表中的变量，生成真正的问题
    # .env.example 中有但文档中没有（排除 ENV_EXAMPLE_ONLY_VARS）
    unexpected_example_not_doc = result.in_example_not_in_doc - ENV_EXAMPLE_ONLY_VARS
    if unexpected_example_not_doc:
        result.errors.append({
            "type": "env_example_not_in_doc",
            "vars": sorted(unexpected_example_not_doc),
            "message": f".env.example 中存在但文档未记录的变量: {', '.join(sorted(unexpected_example_not_doc))}",
        })

    # 文档中有但 .env.example 中没有（排除 DOC_ONLY_VARS）
    unexpected_doc_not_example = result.in_doc_not_in_example - DOC_ONLY_VARS - CODE_ONLY_VARS
    if unexpected_doc_not_example:
        result.warnings.append({
            "type": "doc_not_in_env_example",
            "vars": sorted(unexpected_doc_not_example),
            "message": f"文档中记录但 .env.example 中未定义的变量: {', '.join(sorted(unexpected_doc_not_example))}",
        })

    # 代码中有但文档中没有（排除 CODE_ONLY_VARS）
    unexpected_code_not_doc = result.in_code_not_in_doc - CODE_ONLY_VARS
    if unexpected_code_not_doc:
        result.errors.append({
            "type": "code_not_in_doc",
            "vars": sorted(unexpected_code_not_doc),
            "message": f"代码中使用但文档未记录的变量: {', '.join(sorted(unexpected_code_not_doc))}",
        })

    # 文档中有但代码中没有（排除 DOC_ONLY_VARS 和 ENV_EXAMPLE_ONLY_VARS）
    unexpected_doc_not_code = result.in_doc_not_in_code - DOC_ONLY_VARS - ENV_EXAMPLE_ONLY_VARS
    if unexpected_doc_not_code:
        result.warnings.append({
            "type": "doc_not_in_code",
            "vars": sorted(unexpected_doc_not_code),
            "message": f"文档中记录但代码中未使用的变量: {', '.join(sorted(unexpected_doc_not_code))}",
        })

    return result


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 .env.example、环境变量文档和 config.py 中的环境变量一致性"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式，warning 也会导致失败",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 格式输出",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细信息",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认自动检测）",
    )
    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = args.project_root.resolve()
    else:
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent.parent  # scripts/ci/ 的父父目录

    if not project_root.exists():
        print(f"[ERROR] 项目根目录不存在: {project_root}", file=sys.stderr)
        return 1

    # 文件路径
    env_example_path = project_root / ".env.example"
    env_doc_path = project_root / "docs" / "reference" / "environment_variables.md"
    src_engram_dir = project_root / "src" / "engram"

    if not args.json:
        print(f"项目根目录: {project_root}")
        print(f".env.example: {env_example_path.exists()}")
        print(f"环境变量文档: {env_doc_path.exists()}")
        print(f"src/engram 目录: {src_engram_dir.exists()}")
        print()

    # 解析各来源
    env_example_vars = parse_env_example(env_example_path)
    doc_vars = parse_env_doc(env_doc_path)
    code_vars = parse_config_py_files(src_engram_dir)

    if args.verbose and not args.json:
        print(f".env.example 变量数: {len(env_example_vars)}")
        print(f"文档变量数: {len(doc_vars)}")
        print(f"代码变量数: {len(code_vars)}")
        print()

    # 分析一致性
    result = analyze_consistency(env_example_vars, doc_vars, code_vars)

    # 输出结果
    if args.json:
        output = {
            "ok": not result.has_errors() and (not args.strict or not result.has_warnings()),
            "error_count": len(result.errors),
            "warning_count": len(result.warnings),
            "errors": result.errors,
            "warnings": result.warnings,
            "stats": {
                "env_example_count": len(env_example_vars),
                "doc_count": len(doc_vars),
                "code_count": len(code_vars),
            },
        }
        if args.verbose:
            output["details"] = {
                "env_example_vars": sorted(env_example_vars),
                "doc_vars": sorted(doc_vars),
                "code_vars": sorted(code_vars),
                "in_example_not_in_doc": sorted(result.in_example_not_in_doc),
                "in_doc_not_in_example": sorted(result.in_doc_not_in_example),
                "in_example_not_in_code": sorted(result.in_example_not_in_code),
                "in_code_not_in_example": sorted(result.in_code_not_in_example),
                "in_doc_not_in_code": sorted(result.in_doc_not_in_code),
                "in_code_not_in_doc": sorted(result.in_code_not_in_doc),
            }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("环境变量一致性检查")
        print("=" * 70)
        print()

        if not result.errors and not result.warnings:
            print("[OK] 所有检查通过，环境变量定义一致")
        else:
            # 打印错误
            for error in result.errors:
                print(f"[ERROR] {error['message']}")
                if args.verbose:
                    for var in error["vars"]:
                        print(f"    - {var}")
                print()

            # 打印警告
            for warning in result.warnings:
                print(f"[WARN] {warning['message']}")
                if args.verbose:
                    for var in warning["vars"]:
                        print(f"    - {var}")
                print()

        print("-" * 70)
        print(f"错误: {len(result.errors)}")
        print(f"警告: {len(result.warnings)}")
        print()

        if result.has_errors():
            print("[FAIL] 存在 error 级别问题")
        elif args.strict and result.has_warnings():
            print("[FAIL] 严格模式：存在 warning 级别问题")
        else:
            print("[OK] 检查通过")

    # 退出码
    if result.has_errors():
        return 1
    if args.strict and result.has_warnings():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
