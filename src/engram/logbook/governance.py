"""
engram_logbook.governance - 治理设置与审计模块

提供项目级治理设置管理和写入审计功能。

evidence_refs_json 统一结构规范:
{
    "patches": [
        {
            "artifact_uri": "memory://patch_blobs/<source_type>/<source_id>/<sha256>",
            "sha256": "<content_sha256>",
            "source_id": "<repo_id>:<rev/sha>",
            "source_type": "<svn|git>",
            "kind": "patch"
        },
        ...
    ],
    "attachments": [...],  # 可选，其他附件
    ...                    # 可扩展其他证据类型
}
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, cast

import psycopg
from typing_extensions import NotRequired, TypedDict

from .config import Config
from .db import get_connection
from .errors import DatabaseError, ValidationError
from .uri import (
    EvidenceRefsJson as UriEvidenceRefsJson,
)
from .uri import (
    PatchRef,
    build_evidence_refs_json,
    validate_evidence_ref,
)

# === TypedDict 定义：settings 表行结构 ===


class SettingsRow(TypedDict, total=False):
    """settings 表行结构"""

    project_key: str
    team_write_enabled: bool
    policy_json: Dict[str, Any]
    updated_by: Optional[str]
    updated_at: datetime


# === TypedDict 定义：evidence_refs_json 结构 ===


class PatchEvidenceRef(TypedDict, total=False):
    """Patch 证据引用"""

    artifact_uri: str  # memory://patch_blobs/<source_type>/<source_id>/<sha256>
    sha256: str
    source_id: str  # <repo_id>:<rev/sha>
    source_type: str  # svn | git
    kind: str  # patch


class AttachmentEvidenceRef(TypedDict, total=False):
    """附件证据引用"""

    artifact_uri: str
    sha256: str
    filename: str
    content_type: str


class ExternalEvidenceRef(TypedDict):
    """外部证据引用"""

    uri: str
    description: NotRequired[str]


class EvidenceRefsJson(TypedDict, total=False):
    """evidence_refs_json 完整结构"""

    patches: List[PatchEvidenceRef]
    attachments: List[AttachmentEvidenceRef]
    external: List[ExternalEvidenceRef]


# === TypedDict 定义：write_audit 表行结构 ===


class WriteAuditRow(TypedDict, total=False):
    """write_audit 表行结构"""

    audit_id: int
    actor_user_id: Optional[str]
    target_space: str
    action: str  # allow | redirect | reject
    reason: Optional[str]
    payload_sha: Optional[str]
    evidence_refs_json: EvidenceRefsJson
    created_at: datetime


def _validate_policy_json(policy_json: Any) -> Dict:
    """
    校验 policy_json 必须是 object（dict）

    Args:
        policy_json: 待校验的 policy 数据

    Returns:
        校验通过的 dict

    Raises:
        ValidationError: 如果不是 dict 类型
    """
    if policy_json is None:
        return {}
    if not isinstance(policy_json, dict):
        raise ValidationError(
            "policy_json 必须是 object 类型",
            {"actual_type": type(policy_json).__name__},
        )
    return policy_json


def get_settings(
    project_key: str,
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
) -> Optional[SettingsRow]:
    """
    从 settings 获取项目设置

    Args:
        project_key: 项目键名
        config: 配置实例
        dsn: 数据库 DSN（可选）

    Returns:
        设置字典（SettingsRow），不存在返回 None
    """
    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT project_key, team_write_enabled, policy_json, updated_by, updated_at
                FROM settings
                WHERE project_key = %s
                """,
                (project_key,),
            )
            row = cur.fetchone()
            if row:
                result: SettingsRow = {
                    "project_key": str(row[0]),
                    "team_write_enabled": bool(row[1]),
                    "policy_json": row[2] if isinstance(row[2], dict) else {},
                    "updated_by": str(row[3]) if row[3] else None,
                    "updated_at": row[4],
                }
                return result
            return None
    except psycopg.Error as e:
        raise DatabaseError(
            f"获取 governance settings 失败: {e}",
            {"project_key": project_key, "error": str(e)},
        )
    finally:
        conn.close()


def get_or_create_settings(
    project_key: str,
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
) -> SettingsRow:
    """
    获取或创建项目设置（若不存在则插入默认行）

    使用 ON CONFLICT 保证并发安全：多个并发调用不会报错，均返回一致结果。

    Args:
        project_key: 项目键名
        config: 配置实例
        dsn: 数据库 DSN（可选）

    Returns:
        设置字典（SettingsRow）

    Raises:
        DatabaseError: 数据库操作失败时抛出
    """
    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            # 使用 ON CONFLICT DO NOTHING 保证并发安全
            # 如果已存在则不做任何事（不更新），避免覆盖已有设置
            cur.execute(
                """
                INSERT INTO settings
                    (project_key, team_write_enabled, policy_json, updated_at)
                VALUES (%s, false, '{}', now())
                ON CONFLICT (project_key) DO NOTHING
                """,
                (project_key,),
            )
            conn.commit()

            # 读取当前设置（无论是新插入的还是已存在的）
            cur.execute(
                """
                SELECT project_key, team_write_enabled, policy_json, updated_by, updated_at
                FROM settings
                WHERE project_key = %s
                """,
                (project_key,),
            )
            row = cur.fetchone()
            if row:
                result: SettingsRow = {
                    "project_key": str(row[0]),
                    "team_write_enabled": bool(row[1]),
                    "policy_json": row[2] if isinstance(row[2], dict) else {},
                    "updated_by": str(row[3]) if row[3] else None,
                    "updated_at": row[4],
                }
                return result
            # 理论上不应到达这里，因为刚刚插入或已存在
            default_result: SettingsRow = {
                "project_key": project_key,
                "team_write_enabled": False,
                "policy_json": {},
                "updated_by": None,
                "updated_at": None,  # type: ignore[typeddict-item]
            }
            return default_result
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"获取或创建 governance settings 失败: {e}",
            {"project_key": project_key, "error": str(e)},
        )
    finally:
        conn.close()


def upsert_settings(
    project_key: str,
    team_write_enabled: bool,
    policy_json: Optional[Dict] = None,
    updated_by: Optional[str] = None,
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
) -> bool:
    """
    在 settings 中插入或更新项目设置（upsert）

    Args:
        project_key: 项目键名
        team_write_enabled: 是否启用团队写入
        policy_json: 策略 JSON（必须是 object 类型）
        updated_by: 更新者用户 ID
        config: 配置实例

    Returns:
        True 表示成功

    Raises:
        ValidationError: policy_json 不是 object 类型时抛出
        DatabaseError: 数据库操作失败时抛出
    """
    # 校验 policy_json 必须是 object
    validated_policy = _validate_policy_json(policy_json)

    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO settings
                    (project_key, team_write_enabled, policy_json, updated_by, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (project_key) DO UPDATE
                SET team_write_enabled = EXCLUDED.team_write_enabled,
                    policy_json = EXCLUDED.policy_json,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                """,
                (project_key, team_write_enabled, json.dumps(validated_policy), updated_by),
            )
            conn.commit()
            return True
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"更新 governance settings 失败: {e}",
            {"project_key": project_key, "error": str(e)},
        )
    finally:
        conn.close()


class GovernanceSettings:
    """治理设置便捷包装（供外部调用）"""

    def __init__(self, dsn: Optional[str] = None, config: Optional[Config] = None):
        if isinstance(dsn, Config) and config is None:
            config = dsn
            dsn = None
        self._dsn = dsn
        self._config = config or (Config.from_env() if dsn else None)

    def get(self, key: str, project_key: str) -> Optional[Any]:
        settings = get_settings(project_key, config=self._config, dsn=self._dsn)
        if not settings:
            return None
        if key == "team_write_enabled":
            return settings.get("team_write_enabled")
        policy = settings.get("policy_json") or {}
        return policy.get(key)

    def set(
        self,
        key: str,
        value: Any,
        project_key: str,
        updated_by: Optional[str] = None,
    ) -> bool:
        settings = get_or_create_settings(project_key, config=self._config, dsn=self._dsn)
        policy = settings.get("policy_json") or {}
        team_write_enabled = settings.get("team_write_enabled", False)
        if key == "team_write_enabled":
            team_write_enabled = bool(value)
        else:
            policy[key] = value
        return upsert_settings(
            project_key=project_key,
            team_write_enabled=team_write_enabled,
            policy_json=policy,
            updated_by=updated_by,
            config=self._config,
            dsn=self._dsn,
        )


def query_write_audit(
    since: Optional[str] = None,
    limit: int = 50,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    target_space: Optional[str] = None,
    reason_prefix: Optional[str] = None,
    config: Optional[Config] = None,
) -> List[WriteAuditRow]:
    """
    查询 write_audit 审计记录

    Args:
        since: 起始时间（ISO 8601 格式，例如 2024-01-01T00:00:00Z）
        limit: 返回记录数量上限（默认 50，最大 1000）
        actor: 按 actor_user_id 筛选
        action: 按 action 筛选 (allow/redirect/reject)
        target_space: 按 target_space 筛选
        reason_prefix: 按 reason 前缀筛选（例如 "policy:" 或 "outbox_flush_"）
        config: 配置实例

    Returns:
        审计记录列表（List[WriteAuditRow]）
    """
    # 限制最大返回数量
    limit = min(limit, 1000)

    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            # 构建查询条件
            conditions: List[str] = []
            params: List[Any] = []

            if since:
                conditions.append("created_at >= %s")
                params.append(since)

            if actor:
                conditions.append("actor_user_id = %s")
                params.append(actor)

            if action:
                conditions.append("action = %s")
                params.append(action)

            if target_space:
                conditions.append("target_space = %s")
                params.append(target_space)

            if reason_prefix:
                conditions.append("reason LIKE %s")
                params.append(f"{reason_prefix}%")

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            query = f"""
                SELECT audit_id, actor_user_id, target_space, action, reason,
                       payload_sha, evidence_refs_json, created_at
                FROM write_audit
                {where_clause}
                ORDER BY created_at DESC
                LIMIT %s
            """
            params.append(limit)

            cur.execute(query, params)
            rows = cur.fetchall()

            results: List[WriteAuditRow] = []
            for row in rows:
                audit_row: WriteAuditRow = {
                    "audit_id": int(row[0]),
                    "actor_user_id": str(row[1]) if row[1] else None,
                    "target_space": str(row[2]),
                    "action": str(row[3]),
                    "reason": str(row[4]) if row[4] else None,
                    "payload_sha": str(row[5]) if row[5] else None,
                    "evidence_refs_json": cast(EvidenceRefsJson, row[6]) if row[6] else {},
                    "created_at": row[7],
                }
                results.append(audit_row)
            return results
    except psycopg.Error as e:
        raise DatabaseError(
            f"查询 write_audit 失败: {e}",
            {"error": str(e)},
        )
    finally:
        conn.close()


def _validate_evidence_refs_json(evidence_refs: Union[EvidenceRefsJson, Dict[str, Any]]) -> None:
    """
    验证 evidence_refs_json 结构是否符合规范

    Args:
        evidence_refs: evidence_refs_json 字典

    Raises:
        ValidationError: 结构不符合规范时抛出
    """
    if not isinstance(evidence_refs, dict):
        raise ValidationError(
            "evidence_refs_json 必须是 dict 类型",
            {"actual_type": type(evidence_refs).__name__},
        )

    # 验证 patches 列表（如果存在）
    patches = evidence_refs.get("patches")
    if patches is not None:
        if not isinstance(patches, list):
            raise ValidationError(
                "evidence_refs_json.patches 必须是 list 类型",
                {"actual_type": type(patches).__name__},
            )
        for i, ref in enumerate(patches):
            if not isinstance(ref, dict):
                raise ValidationError(
                    f"evidence_refs_json.patches[{i}] 必须是 dict 类型",
                    {"actual_type": type(ref).__name__},
                )
            is_valid, error = validate_evidence_ref(ref)
            if not is_valid:
                raise ValidationError(
                    f"evidence_refs_json.patches[{i}] 验证失败: {error}",
                    {"index": i, "ref": ref},
                )

    # 验证 attachments 列表（如果存在）
    attachments = evidence_refs.get("attachments")
    if attachments is not None:
        if not isinstance(attachments, list):
            raise ValidationError(
                "evidence_refs_json.attachments 必须是 list 类型",
                {"actual_type": type(attachments).__name__},
            )
        from .uri import parse_attachment_evidence_uri_strict

        for i, ref in enumerate(attachments):
            if not isinstance(ref, dict):
                raise ValidationError(
                    f"evidence_refs_json.attachments[{i}] 必须是 dict 类型",
                    {"actual_type": type(ref).__name__},
                )
            artifact_uri = ref.get("artifact_uri")
            sha256 = ref.get("sha256")
            if not artifact_uri:
                raise ValidationError(
                    f"evidence_refs_json.attachments[{i}] 缺少必需字段: artifact_uri",
                    {"index": i, "ref": ref},
                )
            if not sha256:
                raise ValidationError(
                    f"evidence_refs_json.attachments[{i}] 缺少必需字段: sha256",
                    {"index": i, "ref": ref},
                )
            parsed = parse_attachment_evidence_uri_strict(artifact_uri)
            if not parsed.success:
                raise ValidationError(
                    f"evidence_refs_json.attachments[{i}] artifact_uri 无效: {parsed.error_message}",
                    {"index": i, "ref": ref, "error_code": parsed.error_code},
                )
            if parsed.sha256 and parsed.sha256.lower() != str(sha256).lower():
                raise ValidationError(
                    f"evidence_refs_json.attachments[{i}] sha256 与 artifact_uri 不一致",
                    {"index": i, "ref": ref, "parsed_sha256": parsed.sha256},
                )

    # 验证 external 列表（如果存在）
    external = evidence_refs.get("external")
    if external is not None:
        if not isinstance(external, list):
            raise ValidationError(
                "evidence_refs_json.external 必须是 list 类型",
                {"actual_type": type(external).__name__},
            )
        for i, ref in enumerate(external):
            if not isinstance(ref, dict):
                raise ValidationError(
                    f"evidence_refs_json.external[{i}] 必须是 dict 类型",
                    {"actual_type": type(ref).__name__},
                )
            if not ref.get("uri"):
                raise ValidationError(
                    f"evidence_refs_json.external[{i}] 缺少必需字段: uri",
                    {"index": i, "ref": ref},
                )


def insert_write_audit(
    actor_user_id: Optional[str],
    target_space: str,
    action: str,
    reason: Optional[str] = None,
    payload_sha: Optional[str] = None,
    evidence_refs_json: Optional[Union[EvidenceRefsJson, Dict[str, Any]]] = None,
    config: Optional[Config] = None,
    validate_refs: bool = False,
) -> int:
    """
    在 write_audit 中插入写入审计记录

    Args:
        actor_user_id: 操作者用户 ID
        target_space: 目标空间 (team:<project> / private:<user> / org:shared)
        action: 操作类型 (allow/redirect/reject)
        reason: 操作原因
        payload_sha: 负载 SHA256 哈希
        evidence_refs_json: 证据引用 JSON（推荐使用 build_evidence_refs_json 构建）
        config: 配置实例
        validate_refs: 是否验证 evidence_refs_json 结构（默认 False）

    Returns:
        创建的 audit_id

    Raises:
        ValidationError: evidence_refs_json 结构不符合规范时抛出（需 validate_refs=True）
        DatabaseError: 数据库操作失败时抛出

    evidence_refs_json 推荐结构:
        {
            "patches": [
                {
                    "artifact_uri": "memory://patch_blobs/<source_type>/<source_id>/<sha256>",
                    "sha256": "<content_sha256>",
                    "source_id": "<repo_id>:<rev/sha>",
                    "source_type": "<svn|git>",
                    "kind": "patch"
                }
            ]
        }

    使用示例:
        from engram.logbook.uri import build_evidence_ref_for_patch_blob, build_evidence_refs_json

        ref = build_evidence_ref_for_patch_blob("git", "1:abc123", sha256)
        evidence = build_evidence_refs_json(patches=[ref])
        insert_write_audit(actor, space, "allow", evidence_refs_json=evidence)
    """
    evidence_refs = evidence_refs_json or {}

    # 可选验证
    if validate_refs and evidence_refs:
        _validate_evidence_refs_json(evidence_refs)

    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            if actor_user_id:
                cur.execute(
                    """
                    INSERT INTO identity.users (user_id, display_name)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (actor_user_id, actor_user_id),
                )
            cur.execute(
                """
                INSERT INTO write_audit
                    (actor_user_id, target_space, action, reason, payload_sha, evidence_refs_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING audit_id
                """,
                (
                    actor_user_id,
                    target_space,
                    action,
                    reason,
                    payload_sha,
                    json.dumps(evidence_refs),
                ),
            )
            result = cur.fetchone()
            conn.commit()
            if result is None:
                raise DatabaseError(
                    "插入 write_audit 失败: 未返回 audit_id",
                    {"target_space": target_space, "action": action},
                )
            return int(result[0])
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"插入 write_audit 失败: {e}",
            {"target_space": target_space, "action": action, "error": str(e)},
        )
    finally:
        conn.close()


def write_audit(
    target_space: str,
    action: str,
    actor_user_id: Optional[str] = None,
    reason: Optional[str] = None,
    payload_sha: Optional[str] = None,
    patch_refs: Optional[List[PatchRef]] = None,
    extra_evidence: Optional[Dict[str, Any]] = None,
    config: Optional[Config] = None,
) -> int:
    """
    便捷的审计写入函数，自动构建 evidence_refs_json

    此函数是 insert_write_audit 的便捷封装，自动处理 evidence_refs_json 的构建。

    Args:
        target_space: 目标空间 (team:<project> / private:<user> / org:shared)
        action: 操作类型 (allow/redirect/reject)
        actor_user_id: 操作者用户 ID（可选）
        reason: 操作原因（可选）
        payload_sha: 负载 SHA256 哈希（可选）
        patch_refs: patch 证据引用列表（每项由 build_evidence_ref_for_patch_blob 生成）
        extra_evidence: 额外的证据字段（会合并到 evidence_refs_json 中）
        config: 配置实例

    Returns:
        创建的 audit_id

    使用示例:
        from engram.logbook.uri import build_evidence_ref_for_patch_blob

        refs = [
            build_evidence_ref_for_patch_blob("git", "1:abc123", sha1),
            build_evidence_ref_for_patch_blob("git", "1:def456", sha2),
        ]
        audit_id = write_audit(
            target_space="team:my_project",
            action="allow",
            actor_user_id="user_001",
            reason="scm_sync_batch",
            patch_refs=refs,
        )
    """
    # 使用统一的构建函数
    evidence_refs: UriEvidenceRefsJson = build_evidence_refs_json(
        patches=patch_refs,
        extra=extra_evidence,
    )

    # 转换为本地 EvidenceRefsJson 类型（兼容 insert_write_audit 参数类型）
    evidence_dict: Dict[str, Any] = dict(evidence_refs) if evidence_refs else {}

    return insert_write_audit(
        actor_user_id=actor_user_id,
        target_space=target_space,
        action=action,
        reason=reason,
        payload_sha=payload_sha,
        evidence_refs_json=evidence_dict if evidence_dict else None,
        config=config,
    )
