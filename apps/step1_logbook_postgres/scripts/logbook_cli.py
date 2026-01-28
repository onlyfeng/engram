#!/usr/bin/env python3
"""
Logbook CLI - Command line interface for logbook operations.

Subcommands:
    create_item     - Create a new item in logbook.items
    add_event       - Add an event to logbook.events
    attach          - Add an attachment to logbook.attachments
    set_kv          - Set a key-value pair in logbook.kv
    health          - Check database connection and table existence
    validate        - Validate data integrity (item_id refs, sha256 format, status values)
    render_views    - Generate manifest.csv and index.md views
    governance_get  - Get project governance settings
    governance_set  - Set project governance configuration
    audit_query     - Query write audit logs

All commands output structured JSON results:
    - Success: {ok: true, ...}
    - Failure: {ok: false, code, message, detail}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from engram_step1 import db
from engram_step1.config import add_config_argument, get_config
from engram_step1.errors import (
    EngramError,
    ValidationError,
    make_success_result,
    make_error_result,
)
from engram_step1.governance import get_settings, upsert_settings, query_write_audit
from engram_step1.hashing import get_file_info
from engram_step1.io import (
    add_output_arguments,
    get_output_options,
    log_info,
    log_error,
    output_json,
)
from engram_step1.uri import (
    parse_uri,
    resolve_to_local_path as uri_resolve_to_local_path,
    UriType,
)


def get_artifacts_root() -> str:
    """
    获取 artifacts 根目录

    优先级:
    1. 配置文件中的 artifacts_root
    2. 环境变量 ENGRAM_ARTIFACTS_ROOT
    3. 默认值 ./.agentx/artifacts
    """
    import os
    try:
        cfg = get_config()
        return cfg.get("artifacts_root", os.environ.get("ENGRAM_ARTIFACTS_ROOT", "./.agentx/artifacts"))
    except Exception:
        return os.environ.get("ENGRAM_ARTIFACTS_ROOT", "./.agentx/artifacts")


def is_local_file(uri: str, artifacts_root: Optional[str] = None) -> bool:
    """
    检查 URI 是否指向本地文件

    解析规则:
    - file:// scheme -> 直接检查路径
    - http/https/s3/gs/ftp -> 远程，返回 False
    - 无 scheme -> 按 artifacts-root 解析（而非当前工作目录）

    Args:
        uri: URI 字符串
        artifacts_root: 制品根目录（可选，默认自动获取）

    Returns:
        True 如果 uri 是存在的本地文件路径
    """
    parsed = parse_uri(uri)

    # 远程 URI
    if parsed.is_remote:
        return False

    # file:// scheme
    if parsed.uri_type == UriType.FILE:
        return Path(parsed.path).exists()

    # 无 scheme (artifact) -> 按 artifacts-root 解析
    if parsed.uri_type == UriType.ARTIFACT:
        if artifacts_root is None:
            artifacts_root = get_artifacts_root()
        full_path = Path(artifacts_root) / parsed.path
        return full_path.exists()

    return False


def resolve_uri_path(uri: str, artifacts_root: Optional[str] = None) -> Optional[str]:
    """
    解析 URI 到本地文件路径

    解析规则:
    - file:// scheme -> 直接使用路径
    - http/https/s3/gs/ftp -> 远程，返回 None
    - 无 scheme -> 按 artifacts-root 解析（而非当前工作目录）

    Args:
        uri: URI 字符串
        artifacts_root: 制品根目录（可选，默认自动获取）

    Returns:
        本地文件绝对路径，如果不是本地文件或文件不存在返回 None
    """
    if artifacts_root is None:
        artifacts_root = get_artifacts_root()

    return uri_resolve_to_local_path(uri, artifacts_root=artifacts_root)


def load_payload(json_str: Optional[str] = None, json_file: Optional[str] = None) -> Dict[str, Any]:
    """
    从 JSON 字符串或文件加载 payload

    Args:
        json_str: JSON 字符串
        json_file: JSON 文件路径

    Returns:
        解析后的字典

    Raises:
        ValidationError: 如果同时提供了两者或解析失败
    """
    if json_str and json_file:
        raise ValidationError("不能同时指定 --json 和 --json-file")

    if json_file:
        path = Path(json_file)
        if not path.exists():
            raise ValidationError(f"JSON 文件不存在: {json_file}", {"path": json_file})
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValidationError(f"JSON 解析失败: {e}", {"path": json_file, "error": str(e)})

    if json_str:
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValidationError(f"JSON 解析失败: {e}", {"error": str(e)})

    return {}


def cmd_create_item(args: argparse.Namespace) -> int:
    """处理 create_item 子命令"""
    opts = get_output_options(args)

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        payload = load_payload(args.json, args.json_file)

        # 从 payload 或命令行参数提取字段
        item_type = args.item_type or payload.get("item_type")
        title = args.title or payload.get("title")
        scope_json = payload.get("scope_json", payload.get("scope", {}))
        status = args.status or payload.get("status", "open")
        owner_user_id = args.owner or payload.get("owner_user_id")

        if not item_type:
            raise ValidationError("item_type 是必需的")
        if not title:
            raise ValidationError("title 是必需的")

        item_id = db.create_item(
            item_type=item_type,
            title=title,
            scope_json=scope_json,
            status=status,
            owner_user_id=owner_user_id,
        )

        result = make_success_result(
            item_id=item_id,
            item_type=item_type,
            title=title,
            status=status,
        )
        output_json(result, **opts)
        return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, **opts)
        return 1


def cmd_add_event(args: argparse.Namespace) -> int:
    """处理 add_event 子命令"""
    opts = get_output_options(args)

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        payload = load_payload(args.json, args.json_file)

        # 从 payload 或命令行参数提取字段
        item_id = args.item_id or payload.get("item_id")
        event_type = args.event_type or payload.get("event_type")
        status_from = args.status_from or payload.get("status_from")
        status_to = args.status_to or payload.get("status_to")
        actor_user_id = args.actor or payload.get("actor_user_id")
        source = args.source or payload.get("source", "tool")

        # payload_json 嵌套在输入 payload 中
        event_payload = payload.get("payload_json", payload.get("payload", {}))

        if not item_id:
            raise ValidationError("item_id 是必需的")
        if not event_type:
            raise ValidationError("event_type 是必需的")

        event_id = db.add_event(
            item_id=int(item_id),
            event_type=event_type,
            payload_json=event_payload,
            status_from=status_from,
            status_to=status_to,
            actor_user_id=actor_user_id,
            source=source,
        )

        result_data = {
            "event_id": event_id,
            "item_id": int(item_id),
            "event_type": event_type,
        }

        if status_to:
            result_data["status_updated"] = True
            result_data["status_to"] = status_to

        result = make_success_result(**result_data)
        output_json(result, **opts)
        return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, **opts)
        return 1


def cmd_attach(args: argparse.Namespace) -> int:
    """处理 attach 子命令"""
    opts = get_output_options(args)

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        payload = load_payload(args.json, args.json_file)

        # 从 payload 或命令行参数提取字段
        item_id = args.item_id or payload.get("item_id")
        kind = args.kind or payload.get("kind")
        uri = args.uri or payload.get("uri")
        meta_json = payload.get("meta_json", payload.get("meta", {}))

        if not item_id:
            raise ValidationError("item_id 是必需的")
        if not kind:
            raise ValidationError("kind 是必需的")
        if not uri:
            raise ValidationError("uri 是必需的")

        # 检查 uri 是否指向本地文件
        sha256 = args.sha256 or payload.get("sha256")
        size_bytes = args.size or payload.get("size_bytes")

        local_path = resolve_uri_path(uri)
        if local_path:
            # 从本地文件计算哈希和大小
            file_info = get_file_info(local_path)
            if not sha256:
                sha256 = file_info["hash"]
            if size_bytes is None:
                size_bytes = file_info["size"]

        if not sha256:
            raise ValidationError("sha256 是必需的（提供该值或将 uri 指向本地文件）")

        attachment_id = db.attach(
            item_id=int(item_id),
            kind=kind,
            uri=uri,
            sha256=sha256,
            size_bytes=size_bytes,
            meta_json=meta_json,
        )

        result_data = {
            "attachment_id": attachment_id,
            "item_id": int(item_id),
            "kind": kind,
            "uri": uri,
            "sha256": sha256,
        }

        if size_bytes is not None:
            result_data["size_bytes"] = size_bytes

        if local_path:
            result_data["local_file"] = True

        result = make_success_result(**result_data)
        output_json(result, **opts)
        return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, **opts)
        return 1


def cmd_set_kv(args: argparse.Namespace) -> int:
    """处理 set_kv 子命令"""
    opts = get_output_options(args)

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        payload = load_payload(args.json, args.json_file)

        # 从 payload 或命令行参数提取字段
        namespace = args.namespace or payload.get("namespace")
        key = args.key or payload.get("key")

        # value 可以从 --value (JSON 字符串) 或 payload 获取
        if args.value:
            try:
                value = json.loads(args.value)
            except json.JSONDecodeError as e:
                raise ValidationError(f"--value JSON 解析失败: {e}")
        else:
            value = payload.get("value_json", payload.get("value"))

        if not namespace:
            raise ValidationError("namespace 是必需的")
        if not key:
            raise ValidationError("key 是必需的")
        if value is None:
            raise ValidationError("value 是必需的")

        db.set_kv(namespace=namespace, key=key, value_json=value)

        result = make_success_result(
            namespace=namespace,
            key=key,
            upserted=True,
        )
        output_json(result, **opts)
        return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, **opts)
        return 1


def cmd_health(args: argparse.Namespace) -> int:
    """处理 health 子命令 - 检查数据库连接和表存在"""
    opts = get_output_options(args)

    checks = {}
    errors = []
    warnings = []

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        # 检查 1: 数据库连接和 SELECT 1
        try:
            conn = db.get_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                if row and row[0] == 1:
                    checks["connection"] = {"status": "ok", "message": "数据库连接正常"}
                else:
                    checks["connection"] = {"status": "fail", "message": "SELECT 1 返回异常"}
                    errors.append("connection: SELECT 1 返回异常")
        except Exception as e:
            checks["connection"] = {"status": "fail", "message": str(e)}
            errors.append(f"connection: {e}")
            # 连接失败则无法继续检查表
            result = make_error_result(
                code="CONNECTION_ERROR",
                message="数据库连接失败",
                detail={
                    "checks": checks,
                    "errors": errors,
                    "hint": "请检查 PostgreSQL 服务是否运行，以及配置文件中的 DSN 是否正确",
                },
            )
            output_json(result, **opts)
            return 1

        # 检查 2: 核心 schema 存在性检查
        required_schemas = ["identity", "logbook", "scm", "analysis", "governance"]
        schemas_result = {}
        all_schemas_exist = True

        with conn.cursor() as cur:
            for schema_name in required_schemas:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.schemata
                        WHERE schema_name = %s
                    )
                    """,
                    (schema_name,),
                )
                exists = cur.fetchone()[0]
                schemas_result[schema_name] = {"exists": exists}
                if not exists:
                    all_schemas_exist = False
                    errors.append(f"schema_missing: {schema_name}")

        checks["schemas"] = {
            "status": "ok" if all_schemas_exist else "fail",
            "details": schemas_result,
            "message": "所有必需 schema 存在" if all_schemas_exist else f"缺少 schema: {[s for s, v in schemas_result.items() if not v['exists']]}",
        }

        # 检查 3: 核心表存在性检查（扩展列表）
        core_tables = [
            # identity schema
            "identity.users",
            "identity.accounts",
            "identity.role_profiles",
            # logbook schema
            "logbook.items",
            "logbook.events",
            "logbook.attachments",
            "logbook.kv",
            "logbook.outbox_memory",
            # scm schema
            "scm.repos",
            "scm.svn_revisions",
            "scm.git_commits",
            "scm.patch_blobs",
            "scm.mrs",
            "scm.review_events",
            # analysis schema
            "analysis.runs",
            "analysis.knowledge_candidates",
            # governance schema
            "governance.settings",
            "governance.write_audit",
            "governance.promotion_queue",
        ]

        tables_result = {}
        missing_tables = []

        with conn.cursor() as cur:
            for table_full in core_tables:
                schema_name, table_name = table_full.split(".")
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = %s
                    )
                    """,
                    (schema_name, table_name),
                )
                exists = cur.fetchone()[0]
                tables_result[table_full] = {"exists": exists}
                if not exists:
                    missing_tables.append(table_full)
                    errors.append(f"table_missing: {table_full}")

        checks["tables"] = {
            "status": "ok" if not missing_tables else "fail",
            "total": len(core_tables),
            "existing": len(core_tables) - len(missing_tables),
            "missing": missing_tables,
            "details": tables_result,
        }

        # 检查 4: 物化视图存在性检查
        required_matviews = [
            ("scm", "v_facts"),
        ]
        matviews_result = {}
        missing_matviews = []

        with conn.cursor() as cur:
            for schema_name, view_name in required_matviews:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_matviews
                        WHERE schemaname = %s AND matviewname = %s
                    )
                    """,
                    (schema_name, view_name),
                )
                exists = cur.fetchone()[0]
                full_name = f"{schema_name}.{view_name}"
                matviews_result[full_name] = {"exists": exists}
                if not exists:
                    missing_matviews.append(full_name)
                    errors.append(f"matview_missing: {full_name}")

        checks["matviews"] = {
            "status": "ok" if not missing_matviews else "fail",
            "missing": missing_matviews,
            "details": matviews_result,
        }

        # 检查 5: 关键索引存在性检查
        required_indexes = [
            ("logbook", "idx_logbook_events_item_time"),
            ("logbook", "idx_outbox_memory_pending"),
            ("scm", "idx_v_facts_source_id"),
            ("scm", "idx_v_facts_repo_id"),
        ]
        indexes_result = {}
        missing_indexes = []

        with conn.cursor() as cur:
            for schema_name, index_name in required_indexes:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE schemaname = %s AND indexname = %s
                    )
                    """,
                    (schema_name, index_name),
                )
                exists = cur.fetchone()[0]
                full_name = f"{schema_name}.{index_name}"
                indexes_result[full_name] = {"exists": exists}
                if not exists:
                    missing_indexes.append(full_name)
                    warnings.append(f"index_missing: {full_name}")

        checks["indexes"] = {
            "status": "ok" if not missing_indexes else "warn",
            "missing": missing_indexes,
            "details": indexes_result,
        }

        conn.close()

        # 构建结果
        if errors:
            result = make_error_result(
                code="HEALTH_CHECK_FAILED",
                message=f"健康检查失败: {len(errors)} 个错误",
                detail={
                    "checks": checks,
                    "errors": errors,
                    "warnings": warnings,
                    "hint": "请运行 db_migrate.py 执行数据库迁移以创建缺失的表和索引",
                },
            )
            output_json(result, **opts)
            return 1
        else:
            result = make_success_result(
                checks=checks,
                warnings=warnings if warnings else None,
            )
            output_json(result, **opts)
            return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__, "checks": checks},
        )
        output_json(result, **opts)
        return 1


def cmd_validate(args: argparse.Namespace) -> int:
    """处理 validate 子命令 - 校验数据完整性"""
    import re

    opts = get_output_options(args)

    validations = {}
    errors = []

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        conn = db.get_connection()

        # 校验 1: logbook.events.item_id 存在性
        # 检查 events 表中是否有 item_id 指向不存在的 items
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.event_id, e.item_id
                FROM logbook.events e
                LEFT JOIN logbook.items i ON e.item_id = i.item_id
                WHERE i.item_id IS NULL
                LIMIT 100
                """
            )
            orphan_events = cur.fetchall()

            if orphan_events:
                orphan_list = [{"event_id": row[0], "item_id": row[1]} for row in orphan_events]
                validations["events_item_id"] = {
                    "status": "fail",
                    "message": f"发现 {len(orphan_list)} 个孤立事件（item_id 不存在）",
                    "count": len(orphan_list),
                    "samples": orphan_list[:10],  # 只返回前 10 个样本
                }
                errors.append(f"events_item_id: {len(orphan_list)} 个孤立事件")
            else:
                validations["events_item_id"] = {
                    "status": "ok",
                    "message": "所有 events.item_id 均指向有效的 items",
                }

        # 校验 2: logbook.attachments.sha256 格式
        # SHA256 应该是 64 个十六进制字符
        sha256_pattern = re.compile(r"^[a-fA-F0-9]{64}$")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT attachment_id, sha256
                FROM logbook.attachments
                WHERE sha256 !~ '^[a-fA-F0-9]{64}$'
                LIMIT 100
                """
            )
            invalid_sha256 = cur.fetchall()

            if invalid_sha256:
                invalid_list = [{"attachment_id": row[0], "sha256": row[1]} for row in invalid_sha256]
                validations["attachments_sha256"] = {
                    "status": "fail",
                    "message": f"发现 {len(invalid_list)} 个无效 SHA256 格式",
                    "count": len(invalid_list),
                    "samples": invalid_list[:10],
                }
                errors.append(f"attachments_sha256: {len(invalid_list)} 个无效格式")
            else:
                validations["attachments_sha256"] = {
                    "status": "ok",
                    "message": "所有 attachments.sha256 格式有效",
                }

        # 校验 3: logbook.outbox_memory.status 合法性
        # 合法值: pending/sent/dead
        valid_statuses = ["pending", "sent", "dead"]

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT outbox_id, status
                FROM logbook.outbox_memory
                WHERE status NOT IN ('pending', 'sent', 'dead')
                LIMIT 100
                """
            )
            invalid_status = cur.fetchall()

            if invalid_status:
                invalid_list = [{"outbox_id": row[0], "status": row[1]} for row in invalid_status]
                validations["outbox_memory_status"] = {
                    "status": "fail",
                    "message": f"发现 {len(invalid_list)} 个非法 status 值",
                    "count": len(invalid_list),
                    "valid_values": valid_statuses,
                    "samples": invalid_list[:10],
                }
                errors.append(f"outbox_memory_status: {len(invalid_list)} 个非法状态")
            else:
                validations["outbox_memory_status"] = {
                    "status": "ok",
                    "message": "所有 outbox_memory.status 值合法",
                    "valid_values": valid_statuses,
                }

        # 校验 4: attachments_uri_policy - 检查 attachments 表 URI 类型分布与策略
        # 策略: 优先使用 artifact 类型（无 scheme）的本地相对路径
        # 错误码:
        #   ABSOLUTE_PATH_URI - 使用了绝对路径（file:// 或 /开头）
        #   DANGEROUS_URI_SCHEME - 使用了危险 scheme（javascript:, data:, blob:）
        #   REMOTE_URI_NOT_MATERIALIZED - 远程 URI 未物化到本地
        DANGEROUS_SCHEMES = {"javascript", "data", "blob", "vbscript"}
        
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT attachment_id, item_id, kind, uri
                FROM logbook.attachments
                LIMIT 500
                """
            )
            all_attachments = cur.fetchall()

            uri_stats = {
                "artifact": 0,      # 无 scheme 的本地相对路径（推荐）
                "file": 0,          # file:// 本地绝对路径
                "remote": 0,        # http/https/s3/gs/ftp 远程
                "unknown": 0,       # 未知 scheme
                "absolute_path": 0, # 绝对路径（非推荐）
                "dangerous": 0,     # 危险 scheme
            }
            remote_samples = []
            unknown_samples = []
            absolute_path_samples = []
            dangerous_samples = []
            
            # URI 问题详情（带错误码）
            uri_issues = []

            for row in all_attachments:
                attachment_id, item_id, kind, uri = row
                parsed = parse_uri(uri)
                
                # 检查危险 scheme
                if parsed.scheme and parsed.scheme.lower() in DANGEROUS_SCHEMES:
                    uri_stats["dangerous"] += 1
                    issue = {
                        "attachment_id": attachment_id,
                        "item_id": item_id,
                        "kind": kind,
                        "uri": uri,
                        "error_code": "DANGEROUS_URI_SCHEME",
                        "message": f"危险的 URI scheme: {parsed.scheme}",
                        "remedy": f"请移除此附件或替换为安全的 artifact 路径",
                    }
                    uri_issues.append(issue)
                    if len(dangerous_samples) < 10:
                        dangerous_samples.append(issue)
                    continue
                
                # 检查绝对路径（file:// 或以 / 开头的路径）
                is_absolute = False
                if parsed.uri_type == UriType.FILE:
                    is_absolute = True
                    uri_stats["file"] += 1
                elif parsed.uri_type == UriType.ARTIFACT and parsed.path.startswith("/"):
                    is_absolute = True
                
                if is_absolute:
                    uri_stats["absolute_path"] += 1
                    issue = {
                        "attachment_id": attachment_id,
                        "item_id": item_id,
                        "kind": kind,
                        "uri": uri,
                        "error_code": "ABSOLUTE_PATH_URI",
                        "message": "使用了绝对路径，不便于跨环境迁移",
                        "remedy": (
                            f"建议将文件移动到 artifacts 目录，并使用相对路径:\n"
                            f"  1. 移动文件: cp '{uri}' ./.agentx/artifacts/<适当子目录>/\n"
                            f"  2. 更新 URI: UPDATE attachments SET uri='<相对路径>' WHERE attachment_id={attachment_id}"
                        ),
                    }
                    uri_issues.append(issue)
                    if len(absolute_path_samples) < 10:
                        absolute_path_samples.append(issue)
                elif parsed.uri_type == UriType.ARTIFACT:
                    uri_stats["artifact"] += 1
                elif parsed.is_remote:
                    uri_stats["remote"] += 1
                    issue = {
                        "attachment_id": attachment_id,
                        "item_id": item_id,
                        "kind": kind,
                        "uri": uri,
                        "uri_type": parsed.uri_type.value,
                        "error_code": "REMOTE_URI_NOT_MATERIALIZED",
                        "message": "远程 URI 未物化到本地",
                        "remedy": (
                            f"建议下载并物化到本地 artifacts 目录:\n"
                            f"  python scm_materialize_patch_blob.py --attachment-id {attachment_id}"
                        ),
                    }
                    uri_issues.append(issue)
                    if len(remote_samples) < 10:
                        remote_samples.append(issue)
                else:
                    uri_stats["unknown"] += 1
                    issue = {
                        "attachment_id": attachment_id,
                        "item_id": item_id,
                        "kind": kind,
                        "uri": uri,
                        "uri_type": parsed.uri_type.value if hasattr(parsed, 'uri_type') else "unknown",
                        "error_code": "UNKNOWN_URI_TYPE",
                        "message": f"未知的 URI 类型",
                        "remedy": "请检查 URI 格式是否正确",
                    }
                    uri_issues.append(issue)
                    if len(unknown_samples) < 10:
                        unknown_samples.append(issue)

            has_policy_issues = (
                uri_stats["remote"] > 0 or 
                uri_stats["unknown"] > 0 or
                uri_stats["dangerous"] > 0
            )
            has_warnings = uri_stats["absolute_path"] > 0
            
            policy_result = {
                "status": "fail" if uri_stats["dangerous"] > 0 else ("warn" if has_policy_issues or has_warnings else "ok"),
                "message": (
                    f"URI 分布: artifact={uri_stats['artifact']}, file={uri_stats['file']}, "
                    f"remote={uri_stats['remote']}, unknown={uri_stats['unknown']}, "
                    f"absolute_path={uri_stats['absolute_path']}, dangerous={uri_stats['dangerous']}"
                ),
                "stats": uri_stats,
            }

            if uri_issues:
                policy_result["issues"] = uri_issues[:20]  # 最多显示 20 个问题
                policy_result["total_issues"] = len(uri_issues)
            
            # 分类修复提示
            remedies = []
            if dangerous_samples:
                policy_result["dangerous_samples"] = dangerous_samples
                remedies.append("【严重】发现危险 scheme (DANGEROUS_URI_SCHEME)，请立即移除或替换这些附件")
            if absolute_path_samples:
                policy_result["absolute_path_samples"] = absolute_path_samples
                remedies.append("【警告】发现绝对路径 (ABSOLUTE_PATH_URI)，建议转换为相对路径以便跨环境迁移")
            if remote_samples:
                policy_result["remote_samples"] = remote_samples
                remedies.append("【提示】远程 URI 建议下载物化到本地 artifacts 目录")
            if unknown_samples:
                policy_result["unknown_samples"] = unknown_samples
                remedies.append("【提示】未知类型 URI 请检查格式是否正确")
            
            if remedies:
                policy_result["remedy"] = "\n".join(remedies)

            validations["attachments_uri_policy"] = policy_result

            # 危险 scheme 视为严重错误
            if uri_stats["dangerous"] > 0:
                errors.append(
                    f"attachments_uri_policy: 发现 {uri_stats['dangerous']} 个危险 scheme (DANGEROUS_URI_SCHEME)"
                )
            if has_policy_issues:
                errors.append(
                    f"attachments_uri_policy: {uri_stats['remote']} 个远程 URI, "
                    f"{uri_stats['unknown']} 个未知类型"
                )

        # 校验 5: patch_blobs_uri_policy - 检查 patch 类型附件的 blob 物化状态
        # 策略: kind='patch' 的附件应该在本地有物化的 blob 文件
        artifacts_root = get_artifacts_root()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT attachment_id, item_id, uri, sha256
                FROM logbook.attachments
                WHERE kind = 'patch'
                LIMIT 200
                """
            )
            patch_attachments = cur.fetchall()

            patch_stats = {
                "total": len(patch_attachments),
                "materialized": 0,      # 本地已物化
                "not_materialized": 0,  # 本地未物化
                "remote_only": 0,       # 仅有远程 URI
            }
            not_materialized_samples = []

            for row in patch_attachments:
                attachment_id, item_id, uri, sha256 = row
                parsed = parse_uri(uri)

                if parsed.is_remote:
                    # 远程 URI，检查本地是否有物化
                    patch_stats["remote_only"] += 1
                    if len(not_materialized_samples) < 10:
                        not_materialized_samples.append({
                            "attachment_id": attachment_id,
                            "item_id": item_id,
                            "uri": uri,
                            "sha256": sha256[:16] + "..." if sha256 else None,
                            "reason": "remote_uri",
                        })
                else:
                    # 本地 URI，检查文件是否存在
                    local_path = resolve_uri_path(uri, artifacts_root)
                    if local_path:
                        patch_stats["materialized"] += 1
                    else:
                        patch_stats["not_materialized"] += 1
                        if len(not_materialized_samples) < 10:
                            not_materialized_samples.append({
                                "attachment_id": attachment_id,
                                "item_id": item_id,
                                "uri": uri,
                                "sha256": sha256[:16] + "..." if sha256 else None,
                                "reason": "file_not_found",
                            })

            has_patch_issues = (
                patch_stats["not_materialized"] > 0 or patch_stats["remote_only"] > 0
            )

            patch_result = {
                "status": "warn" if has_patch_issues else "ok",
                "message": (
                    f"Patch blobs 状态: 共 {patch_stats['total']} 个, "
                    f"已物化={patch_stats['materialized']}, "
                    f"未物化={patch_stats['not_materialized']}, "
                    f"仅远程={patch_stats['remote_only']}"
                ),
                "stats": patch_stats,
            }

            if not_materialized_samples:
                patch_result["samples"] = not_materialized_samples
                patch_result["remedy"] = (
                    "运行 scm_materialize_patch_blob 命令回填缺失的 patch blob 文件:\n"
                    "  单条处理: python scm_materialize_patch_blob.py --attachment-id <id>\n"
                    "  批量处理: python scm_materialize_patch_blob.py --kind patch --materialize-missing\n"
                    "  重试失败: python scm_materialize_patch_blob.py --retry-failed"
                )

            validations["patch_blobs_uri_policy"] = patch_result

            if has_patch_issues:
                errors.append(
                    f"patch_blobs_uri_policy: {patch_stats['not_materialized']} 个未物化, "
                    f"{patch_stats['remote_only']} 个仅远程"
                )

        conn.close()

        # 校验 6: views_integrity - 检查视图文件是否存在且由工具生成
        # 检查默认视图目录下的文件
        # 错误码:
        #   VIEWS_DIR_NOT_EXISTS - 视图目录不存在
        #   META_FILE_NOT_EXISTS - 元数据文件不存在
        #   META_PARSE_ERROR - 元数据文件解析失败
        #   INVALID_GENERATOR - 生成器标记无效
        #   ARTIFACT_KEY_MISMATCH - artifact key（文件名）不一致
        #   SHA256_MISMATCH - SHA256 哈希不匹配
        #   FILE_MISSING - 文件缺失
        #   MARKER_MISSING - 自动生成标记缺失
        #   EXTRA_FILE_IN_META - 元数据中有多余的文件记录
        from render_views import DEFAULT_OUT_DIR, AUTO_GENERATED_MARKER
        views_dir = Path(DEFAULT_OUT_DIR)
        
        views_result = {
            "status": "ok",
            "views_dir": str(views_dir),
            "files_checked": [],
            "artifact_checks": [],  # artifact key/sha256 一致性检查结果
        }
        views_issues = []
        
        # 预期的视图文件列表（artifact keys）
        EXPECTED_VIEW_FILES = {"manifest.csv", "index.md"}
        
        if not views_dir.exists():
            views_result["status"] = "warn"
            views_result["message"] = f"视图目录不存在: {views_dir}"
            views_issues.append({
                "error_code": "VIEWS_DIR_NOT_EXISTS",
                "message": f"视图目录不存在: {views_dir}",
                "remedy": "运行 logbook_cli.py render_views 生成视图文件",
            })
        else:
            # 检查元数据文件
            meta_path = views_dir / ".views_meta.json"
            if not meta_path.exists():
                views_result["status"] = "warn"
                views_result["message"] = "视图元数据文件不存在，无法验证文件完整性"
                views_issues.append({
                    "error_code": "META_FILE_NOT_EXISTS",
                    "message": "视图元数据文件 .views_meta.json 不存在",
                    "remedy": "运行 logbook_cli.py render_views 重新生成视图文件",
                })
            else:
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta_data = json.load(f)
                    
                    # 验证生成器标记
                    if meta_data.get("generator") != "render_views.py":
                        views_result["status"] = "fail"
                        views_issues.append({
                            "error_code": "INVALID_GENERATOR",
                            "message": f"生成器标记无效: {meta_data.get('generator')}",
                            "expected": "render_views.py",
                            "actual": meta_data.get("generator"),
                            "remedy": "元数据文件可能被篡改，请重新生成视图文件",
                        })
                    
                    # 获取元数据中记录的文件列表
                    meta_files = set(meta_data.get("files", {}).keys())
                    
                    # === artifact key 一致性检查 ===
                    # 检查元数据中的文件名是否与预期一致
                    for expected_key in EXPECTED_VIEW_FILES:
                        artifact_check = {"artifact_key": expected_key}
                        
                        if expected_key not in meta_files:
                            artifact_check["status"] = "missing_in_meta"
                            artifact_check["error_code"] = "ARTIFACT_KEY_MISMATCH"
                            artifact_check["message"] = f"元数据中缺少文件记录: {expected_key}"
                            views_issues.append({
                                "error_code": "ARTIFACT_KEY_MISMATCH",
                                "message": f"元数据中缺少预期文件 '{expected_key}' 的记录",
                                "remedy": "运行 logbook_cli.py render_views 重新生成视图文件",
                            })
                        else:
                            artifact_check["status"] = "present_in_meta"
                        
                        views_result["artifact_checks"].append(artifact_check)
                    
                    # 检查元数据中是否有多余的文件记录
                    extra_files = meta_files - EXPECTED_VIEW_FILES
                    for extra_key in extra_files:
                        views_issues.append({
                            "error_code": "EXTRA_FILE_IN_META",
                            "message": f"元数据中存在未知文件记录: {extra_key}",
                            "artifact_key": extra_key,
                            "remedy": "元数据可能被篡改，请检查或重新生成",
                        })
                        views_result["artifact_checks"].append({
                            "artifact_key": extra_key,
                            "status": "extra",
                            "error_code": "EXTRA_FILE_IN_META",
                        })
                    
                    # === SHA256 一致性检查 ===
                    # 验证各个文件的 hash
                    for filename, file_meta in meta_data.get("files", {}).items():
                        file_path = views_dir / filename
                        file_check = {"file": filename, "artifact_key": filename}
                        
                        if not file_path.exists():
                            file_check["status"] = "missing"
                            file_check["error_code"] = "FILE_MISSING"
                            views_issues.append({
                                "error_code": "FILE_MISSING",
                                "file": filename,
                                "message": f"文件不存在: {filename}",
                                "remedy": f"运行 logbook_cli.py render_views 重新生成文件",
                            })
                        else:
                            # 计算当前文件的 hash
                            current_info = get_file_info(str(file_path))
                            expected_hash = file_meta.get("sha256")
                            expected_size = file_meta.get("size")
                            
                            file_check["expected_sha256"] = expected_hash
                            file_check["actual_sha256"] = current_info["hash"]
                            file_check["expected_size"] = expected_size
                            file_check["actual_size"] = current_info["size"]
                            
                            if expected_hash and current_info["hash"] != expected_hash:
                                file_check["status"] = "sha256_mismatch"
                                file_check["error_code"] = "SHA256_MISMATCH"
                                file_check["expected_sha256_short"] = expected_hash[:16] + "..."
                                file_check["actual_sha256_short"] = current_info["hash"][:16] + "..."
                                views_issues.append({
                                    "error_code": "SHA256_MISMATCH",
                                    "file": filename,
                                    "message": f"SHA256 不匹配: {filename}",
                                    "expected": expected_hash[:16] + "...",
                                    "actual": current_info["hash"][:16] + "...",
                                    "remedy": (
                                        f"文件 {filename} 可能被手动修改，请选择:\n"
                                        f"  1. 恢复自动生成: python logbook_cli.py render_views\n"
                                        f"  2. 如需保留修改，请更新元数据或移除 .views_meta.json"
                                    ),
                                })
                            else:
                                file_check["status"] = "ok"
                        
                        views_result["files_checked"].append(file_check)
                    
                    views_result["meta_rendered_at"] = meta_data.get("rendered_at")
                    views_result["meta_items_count"] = meta_data.get("items_count")
                    
                except json.JSONDecodeError as e:
                    views_result["status"] = "fail"
                    views_result["message"] = f"元数据文件解析失败: {e}"
                    views_issues.append({
                        "error_code": "META_PARSE_ERROR",
                        "message": f"元数据文件 JSON 解析失败: {e}",
                        "remedy": "元数据文件可能损坏，请重新生成视图文件",
                    })
                except Exception as e:
                    views_result["status"] = "warn"
                    views_result["message"] = f"验证视图文件时出错: {e}"
                    views_issues.append({
                        "error_code": "VERIFICATION_ERROR",
                        "message": f"验证过程出错: {e}",
                        "remedy": "请检查文件权限或重新生成视图文件",
                    })
            
            # 检查视图文件中的自动生成标记
            manifest_path = views_dir / "manifest.csv"
            index_path = views_dir / "index.md"
            
            for fpath, marker_style in [(manifest_path, "csv"), (index_path, "md")]:
                if fpath.exists():
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            header = f.read(500)  # 只读前 500 字符检查标记
                        
                        if AUTO_GENERATED_MARKER not in header:
                            views_issues.append({
                                "error_code": "MARKER_MISSING",
                                "file": fpath.name,
                                "message": f"文件缺少自动生成标记: {fpath.name}",
                                "remedy": (
                                    f"文件 {fpath.name} 可能被手动创建或修改，建议:\n"
                                    f"  python logbook_cli.py render_views"
                                ),
                            })
                    except Exception:
                        pass
        
        if views_issues:
            # 判断严重性
            has_fail = any(
                issue.get("error_code") in ("INVALID_GENERATOR", "META_PARSE_ERROR", "SHA256_MISMATCH")
                for issue in views_issues if isinstance(issue, dict)
            )
            views_result["status"] = "fail" if has_fail else "warn"
            views_result["issues"] = views_issues
            views_result["total_issues"] = len(views_issues)
            views_result["remedy"] = (
                "运行 logbook_cli.py render_views 重新生成视图文件:\n"
                "  python logbook_cli.py render_views\n\n"
                "错误码说明:\n"
                "  SHA256_MISMATCH - 文件内容与元数据记录不一致\n"
                "  ARTIFACT_KEY_MISMATCH - 文件名与预期不一致\n"
                "  FILE_MISSING - 预期文件不存在\n"
                "  MARKER_MISSING - 缺少自动生成标记"
            )
            # 视图文件问题不作为严重错误，只记录警告
            # errors.append(f"views_integrity: {len(views_issues)} 个问题")
        else:
            views_result["message"] = "视图文件完整性验证通过（artifact key 和 SHA256 均一致）"
        
        validations["views_integrity"] = views_result

        if errors:
            result = make_error_result(
                code="VALIDATION_FAILED",
                message=f"数据校验失败: {len(errors)} 个问题",
                detail={"validations": validations, "errors": errors},
            )
            output_json(result, **opts)
            return 1
        else:
            result = make_success_result(validations=validations)
            output_json(result, **opts)
            return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__, "validations": validations},
        )
        output_json(result, **opts)
        return 1


def cmd_render_views(args: argparse.Namespace) -> int:
    """处理 render_views 子命令"""
    opts = get_output_options(args)

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        # 验证参数
        if args.log_event and not args.item_id:
            raise ValidationError("使用 --log-event 时必须指定 --item-id")

        # 导入并调用 render_views 函数
        from render_views import render_views

        render_result = render_views(
            out_dir=args.out_dir,
            limit=args.limit,
            item_type=args.item_type,
            status=args.status,
            log_event=args.log_event,
            item_id_for_log=args.item_id,
        )

        result = make_success_result(**render_result)
        output_json(result, **opts)
        return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, **opts)
        return 1


def cmd_governance_get(args: argparse.Namespace) -> int:
    """处理 governance_get 子命令 - 获取项目治理设置"""
    opts = get_output_options(args)

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        if not args.project_key:
            raise ValidationError("project_key 是必需的")

        settings = get_settings(project_key=args.project_key)

        if settings is None:
            result = make_success_result(
                project_key=args.project_key,
                exists=False,
                message="项目设置不存在",
            )
        else:
            # 处理 datetime 序列化
            settings_data = dict(settings)
            if settings_data.get("updated_at"):
                settings_data["updated_at"] = settings_data["updated_at"].isoformat()

            result = make_success_result(
                exists=True,
                settings=settings_data,
            )

        output_json(result, **opts)
        return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, **opts)
        return 1


def cmd_governance_set(args: argparse.Namespace) -> int:
    """处理 governance_set 子命令 - 设置项目治理配置"""
    opts = get_output_options(args)

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        if not args.project_key:
            raise ValidationError("project_key 是必需的")

        if args.team_write_enabled is None:
            raise ValidationError("team_write_enabled 是必需的")

        # 解析 policy_json
        policy_json = None
        if args.policy_json:
            try:
                policy_json = json.loads(args.policy_json)
            except json.JSONDecodeError as e:
                raise ValidationError(f"policy_json 解析失败: {e}")

        # 将字符串 'true'/'false' 转换为布尔值
        team_write_enabled = args.team_write_enabled
        if isinstance(team_write_enabled, str):
            team_write_enabled = team_write_enabled.lower() in ("true", "1", "yes")

        upsert_settings(
            project_key=args.project_key,
            team_write_enabled=team_write_enabled,
            policy_json=policy_json,
            updated_by=args.updated_by,
        )

        result = make_success_result(
            project_key=args.project_key,
            team_write_enabled=team_write_enabled,
            policy_json=policy_json or {},
            updated_by=args.updated_by,
            upserted=True,
        )
        output_json(result, **opts)
        return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, **opts)
        return 1


def cmd_audit_query(args: argparse.Namespace) -> int:
    """处理 audit_query 子命令 - 查询审计日志"""
    opts = get_output_options(args)

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        audits = query_write_audit(
            since=args.since,
            limit=args.limit or 50,
            actor=args.actor,
            action=args.action,
            target_space=args.target_space,
        )

        # 处理 datetime 序列化
        for audit in audits:
            if audit.get("created_at"):
                audit["created_at"] = audit["created_at"].isoformat()

        result = make_success_result(
            count=len(audits),
            audits=audits,
        )
        output_json(result, **opts)
        return 0

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, **opts)
        return 1


def cmd_seek_check(args: argparse.Namespace) -> int:
    """处理 seek_check 子命令 - Step3 索引一致性检查"""
    opts = get_output_options(args)

    try:
        # 初始化配置
        if hasattr(args, "config_path") and args.config_path:
            get_config(args.config_path, reload=True)

        # 验证参数
        if args.sample_ratio is not None and args.limit is not None:
            raise ValidationError("--sample-ratio 和 --limit 不能同时使用")

        if args.sample_ratio is not None and not (0 < args.sample_ratio <= 1.0):
            raise ValidationError("--sample-ratio 必须在 (0.0, 1.0] 范围内")

        if (args.log_event or args.save_attachment) and not args.item_id:
            raise ValidationError("使用 --log-event 或 --save-attachment 时必须指定 --item-id")

        # 导入 Step3 检查模块
        try:
            from step3_seekdb_rag_hybrid.seek_consistency_check import (
                run_consistency_check,
                log_to_logbook,
                add_to_attachments,
            )
            from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
        except ImportError as e:
            raise ValidationError(
                f"无法导入 Step3 模块，请确保 step3_seekdb_rag_hybrid 在 PYTHONPATH 中: {e}"
            )

        # 确定分块版本
        chunking_version = args.chunking_version or CHUNKING_VERSION

        # 获取数据库连接
        conn = db.get_connection()

        try:
            # 执行一致性检查
            check_result = run_consistency_check(
                conn=conn,
                chunking_version=chunking_version,
                project_key=args.project_key,
                sample_ratio=args.sample_ratio,
                limit=args.limit,
                check_artifacts=not args.skip_artifacts,
                verify_sha256=not args.skip_sha256,
            )

            # 记录到 logbook.events
            event_id = None
            if args.log_event:
                event_id = log_to_logbook(
                    conn=conn,
                    item_id=args.item_id,
                    result=check_result,
                    actor_user_id=args.actor,
                )

            # 保存为附件
            attachment_id = None
            if args.save_attachment:
                attachment_id = add_to_attachments(
                    conn=conn,
                    item_id=args.item_id,
                    result=check_result,
                )

            # 提交事务
            conn.commit()

            # 构建输出结果
            output_data = check_result.to_dict()
            if event_id:
                output_data["logged_event_id"] = event_id
            if attachment_id:
                output_data["saved_attachment_id"] = attachment_id

            if check_result.has_issues:
                result = make_error_result(
                    code="CONSISTENCY_ISSUES_FOUND",
                    message=f"发现 {check_result.total_issues} 个一致性问题",
                    detail=output_data,
                )
            else:
                result = make_success_result(**output_data)

            output_json(result, **opts)
            return 0 if not check_result.has_issues else 1

        finally:
            conn.close()

    except EngramError as e:
        output_json(e.to_dict(), **opts)
        return e.exit_code
    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, **opts)
        return 1


def main() -> int:
    """主入口点"""
    parser = argparse.ArgumentParser(
        prog="logbook_cli",
        description="Logbook CLI - 用于 logbook 操作的命令行工具",
    )

    # 添加全局参数
    add_config_argument(parser)
    add_output_arguments(parser)

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # === create_item ===
    p_create = subparsers.add_parser("create_item", help="在 logbook.items 中创建新条目")
    p_create.add_argument("--item-type", "-t", dest="item_type", help="条目类型")
    p_create.add_argument("--title", help="条目标题")
    p_create.add_argument("--status", "-s", default=None, help="条目状态 (default: open)")
    p_create.add_argument("--owner", "-o", help="所有者用户 ID")
    p_create.add_argument("--json", "-j", help="JSON payload 字符串")
    p_create.add_argument("--json-file", "-f", help="JSON payload 文件路径")
    add_config_argument(p_create)
    add_output_arguments(p_create)
    p_create.set_defaults(func=cmd_create_item)

    # === add_event ===
    p_event = subparsers.add_parser("add_event", help="在 logbook.events 中添加事件")
    p_event.add_argument("--item-id", "-i", dest="item_id", type=int, help="条目 ID")
    p_event.add_argument("--event-type", "-t", dest="event_type", help="事件类型")
    p_event.add_argument("--status-from", dest="status_from", help="变更前状态")
    p_event.add_argument("--status-to", dest="status_to", help="变更后状态（会更新 item）")
    p_event.add_argument("--actor", "-a", help="操作者用户 ID")
    p_event.add_argument("--source", default=None, help="事件来源 (default: tool)")
    p_event.add_argument("--json", "-j", help="JSON payload 字符串")
    p_event.add_argument("--json-file", "-f", help="JSON payload 文件路径")
    add_config_argument(p_event)
    add_output_arguments(p_event)
    p_event.set_defaults(func=cmd_add_event)

    # === attach ===
    p_attach = subparsers.add_parser("attach", help="在 logbook.attachments 中添加附件")
    p_attach.add_argument("--item-id", "-i", dest="item_id", type=int, help="条目 ID")
    p_attach.add_argument("--kind", "-k", help="附件类型 (patch/log/report/spec/etc)")
    p_attach.add_argument("--uri", "-u", help="附件 URI")
    p_attach.add_argument("--sha256", help="SHA256 哈希值（本地文件自动计算）")
    p_attach.add_argument("--size", type=int, help="大小（字节，本地文件自动计算）")
    p_attach.add_argument("--json", "-j", help="JSON payload 字符串")
    p_attach.add_argument("--json-file", "-f", help="JSON payload 文件路径")
    add_config_argument(p_attach)
    add_output_arguments(p_attach)
    p_attach.set_defaults(func=cmd_attach)

    # === set_kv ===
    p_kv = subparsers.add_parser("set_kv", help="在 logbook.kv 中设置键值对 (upsert)")
    p_kv.add_argument("--namespace", "-n", help="命名空间")
    p_kv.add_argument("--key", "-k", help="键名")
    p_kv.add_argument("--value", "-v", help="值（JSON 字符串）")
    p_kv.add_argument("--json", "-j", help="JSON payload 字符串")
    p_kv.add_argument("--json-file", "-f", help="JSON payload 文件路径")
    add_config_argument(p_kv)
    add_output_arguments(p_kv)
    p_kv.set_defaults(func=cmd_set_kv)

    # === health ===
    p_health = subparsers.add_parser("health", help="检查数据库连接和表存在性")
    add_config_argument(p_health)
    add_output_arguments(p_health)
    p_health.set_defaults(func=cmd_health)

    # === validate ===
    p_validate = subparsers.add_parser("validate", help="校验数据完整性（item_id 存在性、sha256 格式、status 合法性）")
    add_config_argument(p_validate)
    add_output_arguments(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    # === render_views ===
    p_render = subparsers.add_parser("render_views", help="生成 manifest.csv 和 index.md 视图文件")
    p_render.add_argument(
        "--out-dir", "-o",
        default="./.agentx/logbook/views",
        help="输出目录 (default: ./.agentx/logbook/views)",
    )
    p_render.add_argument(
        "--limit", "-n",
        type=int,
        default=50,
        help="最近条目数量上限 (default: 50)",
    )
    p_render.add_argument("--item-type", "-t", dest="item_type", help="按 item_type 筛选")
    p_render.add_argument("--status", "-s", help="按状态筛选")
    p_render.add_argument(
        "--log-event",
        action="store_true",
        help="写入 render_views 事件记录到 logbook.events",
    )
    p_render.add_argument(
        "--item-id", "-i",
        dest="item_id",
        type=int,
        help="用于记录事件的 item_id（需要 --log-event）",
    )
    add_config_argument(p_render)
    add_output_arguments(p_render)
    p_render.set_defaults(func=cmd_render_views)

    # === governance_get ===
    p_gov_get = subparsers.add_parser("governance_get", help="获取项目治理设置")
    p_gov_get.add_argument(
        "--project-key", "-p",
        dest="project_key",
        required=True,
        help="项目键名",
    )
    add_config_argument(p_gov_get)
    add_output_arguments(p_gov_get)
    p_gov_get.set_defaults(func=cmd_governance_get)

    # === governance_set ===
    p_gov_set = subparsers.add_parser("governance_set", help="设置项目治理配置")
    p_gov_set.add_argument(
        "--project-key", "-p",
        dest="project_key",
        required=True,
        help="项目键名",
    )
    p_gov_set.add_argument(
        "--team-write-enabled",
        dest="team_write_enabled",
        required=True,
        help="是否启用团队写入 (true/false)",
    )
    p_gov_set.add_argument(
        "--policy-json",
        dest="policy_json",
        help="策略 JSON 字符串",
    )
    p_gov_set.add_argument(
        "--updated-by", "-u",
        dest="updated_by",
        help="更新者用户 ID",
    )
    add_config_argument(p_gov_set)
    add_output_arguments(p_gov_set)
    p_gov_set.set_defaults(func=cmd_governance_set)

    # === audit_query ===
    p_audit = subparsers.add_parser("audit_query", help="查询写入审计日志")
    p_audit.add_argument(
        "--since", "-s",
        help="起始时间（ISO 8601 格式，例如 2024-01-01T00:00:00Z）",
    )
    p_audit.add_argument(
        "--limit", "-n",
        type=int,
        default=50,
        help="返回记录数量上限 (default: 50, max: 1000)",
    )
    p_audit.add_argument(
        "--actor", "-a",
        help="按操作者用户 ID 筛选",
    )
    p_audit.add_argument(
        "--action",
        help="按操作类型筛选 (allow/redirect/reject)",
    )
    p_audit.add_argument(
        "--target-space", "-t",
        dest="target_space",
        help="按目标空间筛选 (team:<project> / private:<user> / org:shared)",
    )
    add_config_argument(p_audit)
    add_output_arguments(p_audit)
    p_audit.set_defaults(func=cmd_audit_query)

    # === seek_check ===
    p_seek_check = subparsers.add_parser(
        "seek_check",
        help="Step3 索引一致性检查（检查 patch_blobs 索引状态、制品完整性）",
    )
    p_seek_check.add_argument(
        "--chunking-version",
        type=str,
        default=None,
        help="要检查的分块版本号（默认使用当前版本）",
    )
    p_seek_check.add_argument(
        "--project-key", "-p",
        dest="project_key",
        help="按项目标识筛选",
    )
    p_seek_check.add_argument(
        "--sample-ratio",
        type=float,
        default=None,
        help="抽样比例 (0.0-1.0)，与 --limit 互斥",
    )
    p_seek_check.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="最大检查记录数，与 --sample-ratio 互斥",
    )
    p_seek_check.add_argument(
        "--skip-artifacts",
        action="store_true",
        help="跳过制品文件检查（仅检查索引状态）",
    )
    p_seek_check.add_argument(
        "--skip-sha256",
        action="store_true",
        help="跳过 SHA256 验证",
    )
    p_seek_check.add_argument(
        "--log-event",
        action="store_true",
        help="将检查结果记录到 logbook.events",
    )
    p_seek_check.add_argument(
        "--save-attachment",
        action="store_true",
        help="将检查报告保存为 logbook.attachments",
    )
    p_seek_check.add_argument(
        "--item-id", "-i",
        dest="item_id",
        type=int,
        help="用于记录的 item_id（需要 --log-event 或 --save-attachment）",
    )
    p_seek_check.add_argument(
        "--actor", "-a",
        help="操作者用户 ID（用于 logbook 记录）",
    )
    add_config_argument(p_seek_check)
    add_output_arguments(p_seek_check)
    p_seek_check.set_defaults(func=cmd_seek_check)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
