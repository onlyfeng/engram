"""
logbook_adapter - Logbook engram_logbook 包适配器模块

提供对 engram_logbook 包的封装，将 main/worker 需要的接口转发到
engram_logbook.governance 与 engram_logbook.outbox 模块。

此模块是 Gateway 与 Logbook 包之间的桥梁，统一接口调用方式。
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


# ======================== 数据结构 ========================

@dataclass
class OutboxItem:
    """Outbox 记录数据结构"""
    outbox_id: int
    item_id: Optional[int]
    target_space: str
    payload_md: str
    payload_sha: str
    status: str = "pending"
    retry_count: int = 0
    next_attempt_at: Optional[datetime] = None
    locked_at: Optional[datetime] = None
    locked_by: Optional[str] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OutboxItem":
        """从字典创建 OutboxItem"""
        return cls(
            outbox_id=data["outbox_id"],
            item_id=data.get("item_id"),
            target_space=data["target_space"],
            payload_md=data["payload_md"],
            payload_sha=data["payload_sha"],
            status=data.get("status", "pending"),
            retry_count=data.get("retry_count", 0),
            next_attempt_at=data.get("next_attempt_at"),
            locked_at=data.get("locked_at"),
            locked_by=data.get("locked_by"),
            last_error=data.get("last_error"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

# 从 engram_logbook 导入核心模块
_LOGBOOK_PKG_NAME = "engram_logbook"
try:
    from engram.logbook import governance, outbox
    from engram.logbook.config import Config, get_config
    from engram.logbook.db import get_connection, query_knowledge_candidates as _query_knowledge_candidates, create_item as _create_item
    from engram.logbook.errors import DatabaseError
except ImportError as e:
    raise ImportError(
        f"logbook_adapter 需要 engram_logbook 模块: {e}\n"
        "请先安装:\n"
        "  pip install -e apps/logbook_postgres/scripts"
    )

# ======================== 用户校验策略枚举 ========================

class UnknownActorPolicy:
    """
    未知 actor_user_id 处理策略
    
    用于配置当 actor_user_id 提供但用户不存在时的行为。
    """
    REJECT = "reject"           # 拒绝请求
    DEGRADE = "degrade"         # 降级到 private:unknown 空间
    AUTO_CREATE = "auto_create" # 自动创建用户

# 从 engram_logbook.migrate 导入数据库检查和迁移函数
_DB_MIGRATE_AVAILABLE = False
try:
    from engram.logbook.migrate import run_all_checks, run_migrate
    _DB_MIGRATE_AVAILABLE = True
except ImportError:
    run_all_checks = None
    run_migrate = None


class LogbookDBErrorCode:
    """Logbook DB 相关错误码常量"""
    # DB 检查相关
    SCHEMA_MISSING = "LOGBOOK_DB_SCHEMA_MISSING"
    TABLE_MISSING = "LOGBOOK_DB_TABLE_MISSING"
    INDEX_MISSING = "LOGBOOK_DB_INDEX_MISSING"
    MATVIEW_MISSING = "LOGBOOK_DB_MATVIEW_MISSING"
    STRUCTURE_INCOMPLETE = "LOGBOOK_DB_STRUCTURE_INCOMPLETE"
    
    # 迁移相关
    MIGRATE_NOT_AVAILABLE = "LOGBOOK_DB_MIGRATE_NOT_AVAILABLE"
    MIGRATE_FAILED = "LOGBOOK_DB_MIGRATE_FAILED"
    MIGRATE_PARTIAL = "LOGBOOK_DB_MIGRATE_PARTIAL"
    
    # 连接相关
    CONNECTION_FAILED = "LOGBOOK_DB_CONNECTION_FAILED"
    CHECK_FAILED = "LOGBOOK_DB_CHECK_FAILED"


class LogbookDBCheckError(Exception):
    """
    Logbook DB 检查失败异常
    
    Attributes:
        message: 错误消息
        code: 错误码（LogbookDBErrorCode 常量）
        missing_items: 缺失项详情字典
    """
    
    def __init__(
        self,
        message: str,
        code: str = None,
        missing_items: Dict[str, Any] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code or LogbookDBErrorCode.STRUCTURE_INCOMPLETE
        self.missing_items = missing_items or {}


class LogbookDBCheckResult:
    """Logbook DB 检查结果"""
    
    def __init__(
        self,
        ok: bool,
        checks: Dict[str, Any] = None,
        message: str = None,
    ):
        self.ok = ok
        self.checks = checks or {}
        self.message = message
    
    def get_missing_summary(self) -> str:
        """获取缺失项的摘要信息"""
        missing = []
        for check_name, check_result in self.checks.items():
            if isinstance(check_result, dict) and not check_result.get("ok", True):
                missing_items = check_result.get("missing", [])
                if missing_items:
                    missing.append(f"{check_name}: {missing_items}")
        return "; ".join(missing) if missing else "无"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "ok": self.ok,
            "checks": self.checks,
            "message": self.message,
            "missing_summary": self.get_missing_summary(),
        }


class LogbookAdapter:
    """
    Logbook 数据库适配器
    
    封装 engram_logbook 包的功能，提供统一的接口给 Gateway 的 main 和 worker 使用。
    """

    def __init__(self, dsn: Optional[str] = None, config: Optional[Config] = None):
        """
        初始化适配器
        
        DSN 优先级: 显式参数 > POSTGRES_DSN > TEST_PG_DSN
        
        当显式传入 dsn 参数时，使用强覆盖策略：
        - 直接设置 os.environ['POSTGRES_DSN'] = dsn
        - 确保 engram_logbook 的所有子模块都使用该 DSN
        
        Args:
            dsn: PostgreSQL 连接字符串，为 None 时从 POSTGRES_DSN 或 TEST_PG_DSN 环境变量读取
            config: engram_logbook Config 实例
        """
        self._config = config
        self._dsn = dsn
        
        # 如果显式提供了 dsn，使用强覆盖策略设置到环境变量
        # 确保 engram_logbook 的 get_connection 等函数使用该 DSN
        if dsn:
            os.environ["POSTGRES_DSN"] = dsn

    # ======================== identity.users 用户管理 ========================

    def check_user_exists(self, user_id: str) -> bool:
        """
        检查用户是否存在
        
        Args:
            user_id: 用户标识
            
        Returns:
            True 如果用户存在
        """
        conn = get_connection(config=self._config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM identity.users WHERE user_id = %s",
                    (user_id,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        获取用户信息
        
        Args:
            user_id: 用户标识
            
        Returns:
            用户信息字典，不存在返回 None
        """
        conn = get_connection(config=self._config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, display_name, is_active, roles_json, created_at, updated_at
                    FROM identity.users
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if row:
                    return {
                        "user_id": row[0],
                        "display_name": row[1],
                        "is_active": row[2],
                        "roles_json": row[3],
                        "created_at": row[4],
                        "updated_at": row[5],
                    }
                return None
        finally:
            conn.close()

    def ensure_user(
        self,
        user_id: str,
        display_name: Optional[str] = None,
        is_active: bool = True,
        roles_json: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        确保用户存在（幂等 upsert）
        
        如果用户已存在，返回现有用户信息；
        如果用户不存在，创建新用户并返回。
        
        Args:
            user_id: 用户标识（主键）
            display_name: 显示名称（不提供时使用 user_id）
            is_active: 是否激活（默认 True）
            roles_json: 角色信息（默认 {}）
            
        Returns:
            用户信息字典
        """
        if display_name is None:
            display_name = user_id
        if roles_json is None:
            roles_json = {}
        
        conn = get_connection(config=self._config)
        try:
            with conn.cursor() as cur:
                # 使用 ON CONFLICT 实现幂等 upsert
                cur.execute(
                    """
                    INSERT INTO identity.users (user_id, display_name, is_active, roles_json)
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (user_id) DO UPDATE SET
                        updated_at = now()
                    RETURNING user_id, display_name, is_active, roles_json, created_at, updated_at
                    """,
                    (user_id, display_name, is_active, json.dumps(roles_json)),
                )
                row = cur.fetchone()
                conn.commit()
                
                return {
                    "user_id": row[0],
                    "display_name": row[1],
                    "is_active": row[2],
                    "roles_json": row[3],
                    "created_at": row[4],
                    "updated_at": row[5],
                }
        except Exception as e:
            conn.rollback()
            raise DatabaseError(f"ensure_user 失败: {e}")
        finally:
            conn.close()

    def ensure_account(
        self,
        user_id: str,
        account_type: str,
        account_name: str,
        email: Optional[str] = None,
        aliases_json: Optional[List] = None,
        verified: bool = False,
    ) -> Dict[str, Any]:
        """
        确保账户存在（幂等 upsert）
        
        如果账户已存在，返回现有账户信息；
        如果账户不存在，创建新账户并返回。
        
        注意：调用前需确保 user_id 对应的用户已存在（可先调用 ensure_user）。
        
        Args:
            user_id: 关联的用户标识
            account_type: 账户类型（svn/gitlab/git/email）
            account_name: 账户名称
            email: 邮箱地址（可选）
            aliases_json: 别名列表（默认 []）
            verified: 是否已验证（默认 False）
            
        Returns:
            账户信息字典
        """
        if aliases_json is None:
            aliases_json = []
        
        conn = get_connection(config=self._config)
        try:
            with conn.cursor() as cur:
                # 使用 ON CONFLICT 实现幂等 upsert
                cur.execute(
                    """
                    INSERT INTO identity.accounts (user_id, account_type, account_name, email, aliases_json, verified)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (account_type, account_name) DO UPDATE SET
                        updated_at = now()
                    RETURNING account_id, user_id, account_type, account_name, email, aliases_json, verified, updated_at
                    """,
                    (user_id, account_type, account_name, email, json.dumps(aliases_json), verified),
                )
                row = cur.fetchone()
                conn.commit()
                
                return {
                    "account_id": row[0],
                    "user_id": row[1],
                    "account_type": row[2],
                    "account_name": row[3],
                    "email": row[4],
                    "aliases_json": row[5],
                    "verified": row[6],
                    "updated_at": row[7],
                }
        except Exception as e:
            conn.rollback()
            raise DatabaseError(f"ensure_account 失败: {e}")
        finally:
            conn.close()

    # ======================== governance.settings ========================

    def get_settings(self, project_key: str) -> Optional[Dict[str, Any]]:
        """
        读取治理设置
        
        Args:
            project_key: 项目标识
            
        Returns:
            设置字典 {project_key, team_write_enabled, policy_json, updated_by, updated_at}
            如果不存在返回 None
        """
        return governance.get_settings(project_key, config=self._config)

    def get_or_create_settings(self, project_key: str) -> Dict[str, Any]:
        """
        获取或创建治理设置（默认 team_write_enabled=false, policy_json={}）
        
        使用 engram_logbook.governance.get_or_create_settings 实现，
        依赖 ON CONFLICT 保证并发安全。
        
        Args:
            project_key: 项目标识
            
        Returns:
            设置字典
        """
        return governance.get_or_create_settings(
            project_key=project_key,
            config=self._config,
        )

    def upsert_settings(
        self,
        project_key: str,
        team_write_enabled: bool,
        policy_json: Optional[Dict] = None,
        updated_by: Optional[str] = None,
    ) -> bool:
        """
        更新治理设置
        
        Args:
            project_key: 项目标识
            team_write_enabled: 是否启用团队写入
            policy_json: 策略 JSON
            updated_by: 更新者用户 ID
            
        Returns:
            True 表示成功
        """
        return governance.upsert_settings(
            project_key=project_key,
            team_write_enabled=team_write_enabled,
            policy_json=policy_json,
            updated_by=updated_by,
            config=self._config,
        )

    # ======================== governance.write_audit ========================

    def insert_audit(
        self,
        actor_user_id: Optional[str],
        target_space: str,
        action: str,
        reason: Optional[str] = None,
        payload_sha: Optional[str] = None,
        evidence_refs_json: Optional[Dict] = None,
        validate_refs: bool = False,
    ) -> int:
        """
        写入审计日志
        
        Args:
            actor_user_id: 操作者用户 ID
            target_space: 目标空间 (team:<project> / private:<user> / org:shared)
            action: 操作类型 (allow / redirect / reject)
            reason: 原因说明
            payload_sha: 记忆内容的 SHA256 哈希
            evidence_refs_json: 证据链引用
            validate_refs: 是否校验 evidence_refs 结构（默认 False，向后兼容）
            
        Returns:
            创建的 audit_id
        """
        return governance.insert_write_audit(
            actor_user_id=actor_user_id,
            target_space=target_space,
            action=action,
            reason=reason,
            payload_sha=payload_sha,
            evidence_refs_json=evidence_refs_json,
            validate_refs=validate_refs,
            config=self._config,
        )

    def query_audit(
        self,
        since: Optional[str] = None,
        limit: int = 50,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        target_space: Optional[str] = None,
        reason_prefix: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        查询审计记录
        
        Args:
            since: 起始时间（ISO 8601 格式）
            limit: 返回记录数量上限
            actor: 按 actor_user_id 筛选
            action: 按 action 筛选
            target_space: 按 target_space 筛选
            reason_prefix: 按 reason 前缀筛选（例如 "policy:" 或 "outbox_flush_"）
            
        Returns:
            审计记录列表
        """
        return governance.query_write_audit(
            since=since,
            limit=limit,
            actor=actor,
            action=action,
            target_space=target_space,
            reason_prefix=reason_prefix,
            config=self._config,
        )

    # ======================== logbook.items ========================

    def create_item(
        self,
        item_type: str,
        title: str,
        scope_json: Optional[Dict] = None,
        status: str = "open",
        owner_user_id: Optional[str] = None,
    ) -> int:
        """
        在 logbook.items 中创建新条目
        
        Args:
            item_type: 条目类型（如 'evidence', 'note', 'task' 等）
            title: 标题
            scope_json: 范围元数据（默认 {}）
            status: 状态（默认 'open'）
            owner_user_id: 所有者用户 ID（可选）
            
        Returns:
            创建的 item_id
        """
        return _create_item(
            item_type=item_type,
            title=title,
            scope_json=scope_json,
            status=status,
            owner_user_id=owner_user_id,
            config=self._config,
        )

    # ======================== logbook.outbox_memory ========================

    def check_dedup(
        self,
        target_space: str,
        payload_sha: str,
    ) -> Optional[Dict[str, Any]]:
        """
        检查是否存在已成功写入的重复记录（幂等去重）
        
        Args:
            target_space: 目标空间 (team:<project> / private:<user> / org:shared)
            payload_sha: payload 的 SHA256 哈希
            
        Returns:
            如果存在已成功写入的记录，返回该记录的字典，否则返回 None
        """
        return outbox.check_dedup(
            target_space=target_space,
            payload_sha=payload_sha,
            config=self._config,
        )

    def enqueue_outbox(
        self,
        payload_md: str,
        target_space: str,
        item_id: Optional[int] = None,
        last_error: Optional[str] = None,
    ) -> int:
        """
        将记忆入队到 outbox_memory 表（失败补偿队列）
        
        Args:
            payload_md: Markdown 格式的记忆内容
            target_space: 目标空间
            item_id: 关联的 logbook.items.item_id（可选）
            last_error: 错误信息
            
        Returns:
            创建的 outbox_id
        """
        return outbox.enqueue_memory(
            payload_md=payload_md,
            target_space=target_space,
            item_id=item_id,
            last_error=last_error,
            config=self._config,
        )

    def get_pending_outbox(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取待处理的 outbox 记录
        
        Args:
            limit: 返回记录数量上限
            
        Returns:
            pending 状态的 outbox 记录列表
        """
        return outbox.get_pending(limit=limit, config=self._config)

    def get_outbox_by_id(self, outbox_id: int) -> Optional[Dict[str, Any]]:
        """
        根据 outbox_id 获取单条记录
        
        Args:
            outbox_id: Outbox 记录 ID
            
        Returns:
            outbox 记录字典，不存在返回 None
        """
        return outbox.get_by_id(outbox_id=outbox_id, config=self._config)

    def mark_outbox_sent(self, outbox_id: int) -> bool:
        """
        标记 outbox 记录为已发送 (pending -> sent)
        
        Args:
            outbox_id: Outbox 记录 ID
            
        Returns:
            True 表示成功更新
        """
        return outbox.mark_sent(outbox_id=outbox_id, config=self._config)

    def increment_outbox_retry(
        self,
        outbox_id: int,
        error: str,
        backoff_seconds: int = 60,
    ) -> int:
        """
        增加 outbox 重试计数并更新错误信息
        
        Args:
            outbox_id: Outbox 记录 ID
            error: 本次错误信息
            backoff_seconds: 基础退避秒数
            
        Returns:
            更新后的 retry_count
        """
        return outbox.increment_retry(
            outbox_id=outbox_id,
            error=error,
            backoff_seconds=backoff_seconds,
            config=self._config,
        )

    def mark_outbox_dead(self, outbox_id: int, error: str) -> bool:
        """
        标记 outbox 记录为死信 (pending -> dead)
        
        Args:
            outbox_id: Outbox 记录 ID
            error: 错误信息
            
        Returns:
            True 表示成功更新
        """
        return outbox.mark_dead(outbox_id=outbox_id, error=error, config=self._config)

    # ======================== Lease 协议方法 ========================

    def claim_outbox(
        self,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 60,
    ) -> List[Dict[str, Any]]:
        """
        并发安全地获取并锁定待处理的 outbox 记录（Lease 协议）
        
        Args:
            worker_id: Worker 标识符（用于锁定归属验证）
            limit: 返回记录数量上限
            lease_seconds: 租约有效期（秒）
            
        Returns:
            已锁定的 outbox 记录列表（字典格式）
        """
        return outbox.claim_outbox(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
            config=self._config,
        )

    def ack_sent(
        self,
        outbox_id: int,
        worker_id: str,
        memory_id: Optional[str] = None,
    ) -> bool:
        """
        确认 outbox 记录已成功发送 (pending -> sent)
        
        Args:
            outbox_id: Outbox 记录 ID
            worker_id: Worker 标识符（必须与 claim 时的一致）
            memory_id: 写入 OpenMemory 后返回的 memory_id
            
        Returns:
            True 表示成功更新
        """
        return outbox.ack_sent(
            outbox_id=outbox_id,
            worker_id=worker_id,
            memory_id=memory_id,
            config=self._config,
        )

    def fail_retry(
        self,
        outbox_id: int,
        worker_id: str,
        error: str,
        next_attempt_at: Any,
    ) -> bool:
        """
        标记 outbox 记录处理失败，安排重试
        
        Args:
            outbox_id: Outbox 记录 ID
            worker_id: Worker 标识符（必须与 claim 时的一致）
            error: 本次失败的错误信息
            next_attempt_at: 下次重试时间（datetime 或 ISO 字符串，由调用方计算）
            
        Returns:
            True 表示成功更新
        """
        return outbox.fail_retry(
            outbox_id=outbox_id,
            worker_id=worker_id,
            error=error,
            next_attempt_at=next_attempt_at,
            config=self._config,
        )

    def mark_dead_by_worker(
        self,
        outbox_id: int,
        worker_id: str,
        error: str,
    ) -> bool:
        """
        标记 outbox 记录为死信（带 worker_id 验证）
        
        Args:
            outbox_id: Outbox 记录 ID
            worker_id: Worker 标识符（必须与 claim 时的一致）
            error: 错误信息
            
        Returns:
            True 表示成功更新
        """
        return outbox.mark_dead_by_worker(
            outbox_id=outbox_id,
            worker_id=worker_id,
            error=error,
            config=self._config,
        )

    def renew_lease(
        self,
        outbox_id: int,
        worker_id: str,
    ) -> bool:
        """
        续期 Lease 租约
        
        仅当 status='pending' 且 locked_by 匹配 worker_id 时才执行更新。
        更新 locked_at 和 updated_at 为当前时间，延长租约有效期。
        
        Args:
            outbox_id: Outbox 记录 ID
            worker_id: Worker 标识符（必须与 claim 时的一致）
            
        Returns:
            True 表示成功续期，False 表示未更新
        """
        return outbox.renew_lease(
            outbox_id=outbox_id,
            worker_id=worker_id,
            config=self._config,
        )

    def renew_lease_batch(
        self,
        outbox_ids: List[int],
        worker_id: str,
    ) -> int:
        """
        批量续期 Lease 租约
        
        Args:
            outbox_ids: Outbox 记录 ID 列表
            worker_id: Worker 标识符（必须与 claim 时的一致）
            
        Returns:
            成功续期的记录数
        """
        return outbox.renew_lease_batch(
            outbox_ids=outbox_ids,
            worker_id=worker_id,
            config=self._config,
        )

    # ======================== Analysis 查询 ========================

    def query_knowledge_candidates(
        self,
        keyword: str,
        top_k: int = 10,
        evidence_filter: Optional[str] = None,
        space_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        从 analysis.knowledge_candidates 表按关键词查询知识候选项
        
        使用 ILIKE 对 title 和 content_md 进行模糊匹配。
        用于 OpenMemory 查询失败时的降级回退。
        
        Args:
            keyword: 搜索关键词
            top_k: 返回结果数量上限（默认 10）
            evidence_filter: 可选，按 evidence_refs_json 过滤
            space_filter: 可选，按 space 过滤
            
        Returns:
            知识候选项列表
        """
        return _query_knowledge_candidates(
            keyword=keyword,
            top_k=top_k,
            evidence_filter=evidence_filter,
            space_filter=space_filter,
            config=self._config,
        )

    # ======================== 可靠性报告 ========================

    def get_reliability_report(self) -> Dict[str, Any]:
        """
        获取可靠性统计报告
        
        聚合 logbook.outbox_memory 和 governance.write_audit 表的统计数据。
        报告结构符合 schemas/reliability_report_v1.schema.json。
        
        Returns:
            可靠性报告字典，包含：
            - outbox_stats: outbox_memory 表统计
            - audit_stats: write_audit 表统计
            - v2_evidence_stats: v2 evidence 覆盖率统计
            - content_intercept_stats: 内容拦截统计
            - generated_at: 报告生成时间 (ISO 8601)
        """
        from datetime import datetime, timezone
        
        conn = get_connection(config=self._config)
        try:
            outbox_stats = self._get_outbox_stats(conn)
            audit_stats = self._get_audit_stats(conn)
            v2_evidence_stats = self._get_v2_evidence_stats(conn)
            content_intercept_stats = self._get_content_intercept_stats(conn)
            
            return {
                "outbox_stats": outbox_stats,
                "audit_stats": audit_stats,
                "v2_evidence_stats": v2_evidence_stats,
                "content_intercept_stats": content_intercept_stats,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        finally:
            conn.close()

    def _get_outbox_stats(self, conn) -> Dict[str, Any]:
        """获取 outbox_memory 表统计"""
        with conn.cursor() as cur:
            # 总数和按状态分组
            cur.execute("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending,
                    COUNT(*) FILTER (WHERE status = 'sent') as sent,
                    COUNT(*) FILTER (WHERE status = 'dead') as dead,
                    COALESCE(AVG(retry_count), 0) as avg_retry_count,
                    COALESCE(
                        EXTRACT(EPOCH FROM (now() - MIN(created_at) FILTER (WHERE status = 'pending'))),
                        0
                    ) as oldest_pending_age_seconds
                FROM logbook.outbox_memory
            """)
            row = cur.fetchone()
            
            return {
                "total": row[0],
                "by_status": {
                    "pending": row[1],
                    "sent": row[2],
                    "dead": row[3],
                },
                "avg_retry_count": float(row[4]) if row[4] else 0.0,
                "oldest_pending_age_seconds": float(row[5]) if row[5] else 0.0,
            }

    def _get_audit_stats(self, conn) -> Dict[str, Any]:
        """获取 write_audit 表统计"""
        with conn.cursor() as cur:
            # 总数和按 action 分组
            cur.execute("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE action = 'allow') as allow_count,
                    COUNT(*) FILTER (WHERE action = 'redirect') as redirect_count,
                    COUNT(*) FILTER (WHERE action = 'reject') as reject_count,
                    COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours') as recent_24h
                FROM governance.write_audit
            """)
            row = cur.fetchone()
            
            total = row[0]
            by_action = {
                "allow": row[1],
                "redirect": row[2],
                "reject": row[3],
            }
            recent_24h = row[4]
            
            # 按 reason 前缀分组统计
            cur.execute("""
                SELECT 
                    CASE
                        WHEN reason LIKE 'policy:%%' THEN 'policy'
                        WHEN reason LIKE 'openmemory_write_failed:%%' THEN 'openmemory_write_failed'
                        WHEN reason = 'outbox_flush_success' THEN 'outbox_flush_success'
                        WHEN reason = 'dedup_hit' THEN 'dedup_hit'
                        ELSE 'other'
                    END as reason_category,
                    COUNT(*) as count
                FROM governance.write_audit
                GROUP BY reason_category
            """)
            by_reason = {}
            for reason_row in cur.fetchall():
                by_reason[reason_row[0]] = reason_row[1]
            
            # 确保所有必需的 by_reason 键都存在
            for key in ["policy", "openmemory_write_failed", "outbox_flush_success", "dedup_hit", "other"]:
                if key not in by_reason:
                    by_reason[key] = 0
            
            return {
                "total": total,
                "by_action": by_action,
                "recent_24h": recent_24h,
                "by_reason": by_reason,
            }

    def _get_v2_evidence_stats(self, conn) -> Dict[str, Any]:
        """
        获取 v2 evidence 覆盖率统计
        
        统计 scm.patch_blobs 和 logbook.attachments 表的 evidence_uri 覆盖率。
        """
        with conn.cursor() as cur:
            # patch_blobs 覆盖率（检查表是否存在）
            patch_blobs_stats = {"total": 0, "with_evidence_uri": 0, "coverage_pct": 0.0}
            try:
                cur.execute("""
                    SELECT 
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE evidence_uri IS NOT NULL AND evidence_uri != '') as with_evidence
                    FROM scm.patch_blobs
                """)
                row = cur.fetchone()
                if row:
                    total = row[0]
                    with_evidence = row[1]
                    patch_blobs_stats = {
                        "total": total,
                        "with_evidence_uri": with_evidence,
                        "coverage_pct": round((with_evidence / total * 100) if total > 0 else 0.0, 2),
                    }
            except Exception:
                # 表不存在或查询失败，使用默认值
                pass
            
            # attachments 覆盖率（检查表是否存在）
            attachments_stats = {"total": 0, "with_evidence_uri": 0, "coverage_pct": 0.0}
            try:
                cur.execute("""
                    SELECT 
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE evidence_uri IS NOT NULL AND evidence_uri != '') as with_evidence
                    FROM logbook.attachments
                """)
                row = cur.fetchone()
                if row:
                    total = row[0]
                    with_evidence = row[1]
                    attachments_stats = {
                        "total": total,
                        "with_evidence_uri": with_evidence,
                        "coverage_pct": round((with_evidence / total * 100) if total > 0 else 0.0, 2),
                    }
            except Exception:
                # 表不存在或查询失败，使用默认值
                pass
            
            # 总覆盖率计算
            total_artifacts = patch_blobs_stats["total"] + attachments_stats["total"]
            total_with_evidence = patch_blobs_stats["with_evidence_uri"] + attachments_stats["with_evidence_uri"]
            v2_coverage_pct = round((total_with_evidence / total_artifacts * 100) if total_artifacts > 0 else 0.0, 2)
            
            # 无效 evidence_uri 统计（URI 格式错误的数量）
            invalid_evidence_count = 0
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM (
                        SELECT evidence_uri FROM scm.patch_blobs 
                        WHERE evidence_uri IS NOT NULL AND evidence_uri != ''
                        AND evidence_uri NOT LIKE 's3://%%'
                        AND evidence_uri NOT LIKE 'file://%%'
                        AND evidence_uri NOT LIKE 'http://%%'
                        AND evidence_uri NOT LIKE 'https://%%'
                        UNION ALL
                        SELECT evidence_uri FROM logbook.attachments
                        WHERE evidence_uri IS NOT NULL AND evidence_uri != ''
                        AND evidence_uri NOT LIKE 's3://%%'
                        AND evidence_uri NOT LIKE 'file://%%'
                        AND evidence_uri NOT LIKE 'http://%%'
                        AND evidence_uri NOT LIKE 'https://%%'
                    ) invalid_uris
                """)
                row = cur.fetchone()
                if row:
                    invalid_evidence_count = row[0]
            except Exception:
                pass
            
            # 审计模式统计（最近 7 天）
            # 字段优先级：
            #   - mode: evidence_refs_json->'gateway_event'->'policy'->>'mode' > evidence_refs_json->>'mode'
            #   - with_v2_evidence: patches/attachments 数组存在且非空 > v2_evidence 字段存在
            audit_mode_stats = {
                "total": 0,
                "strict_mode_count": 0,
                "compat_mode_count": 0,
                "with_v2_evidence": 0,
            }
            try:
                cur.execute("""
                    SELECT 
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE 
                            COALESCE(
                                evidence_refs_json->'gateway_event'->'policy'->>'mode',
                                evidence_refs_json->>'mode'
                            ) = 'strict'
                        ) as strict_count,
                        COUNT(*) FILTER (WHERE 
                            COALESCE(
                                evidence_refs_json->'gateway_event'->'policy'->>'mode',
                                evidence_refs_json->>'mode'
                            ) = 'compat'
                            OR (
                                evidence_refs_json->'gateway_event'->'policy'->>'mode' IS NULL
                                AND evidence_refs_json->>'mode' IS NULL
                            )
                        ) as compat_count,
                        COUNT(*) FILTER (WHERE 
                            (evidence_refs_json ? 'patches' AND jsonb_array_length(COALESCE(evidence_refs_json->'patches', '[]'::jsonb)) > 0)
                            OR (evidence_refs_json ? 'attachments' AND jsonb_array_length(COALESCE(evidence_refs_json->'attachments', '[]'::jsonb)) > 0)
                            OR evidence_refs_json->>'v2_evidence' IS NOT NULL
                        ) as with_v2
                    FROM governance.write_audit
                    WHERE created_at > now() - interval '7 days'
                """)
                row = cur.fetchone()
                if row:
                    audit_mode_stats = {
                        "total": row[0],
                        "strict_mode_count": row[1],
                        "compat_mode_count": row[2],
                        "with_v2_evidence": row[3],
                    }
            except Exception:
                pass
            
            return {
                "patch_blobs": patch_blobs_stats,
                "attachments": attachments_stats,
                "v2_coverage_pct": v2_coverage_pct,
                "invalid_evidence_count": invalid_evidence_count,
                "total_with_evidence": total_with_evidence,
                "audit_mode_stats_7d": audit_mode_stats,
            }

    def _get_content_intercept_stats(self, conn) -> Dict[str, Any]:
        """
        获取内容拦截统计（diff/log 拦截次数）
        
        从 governance.write_audit 表中统计因内容策略被拦截的记录。
        """
        with conn.cursor() as cur:
            # 统计拦截次数
            try:
                cur.execute("""
                    SELECT 
                        COUNT(*) FILTER (WHERE reason LIKE '%%diff%%' AND reason NOT LIKE '%%log%%') as diff_reject,
                        COUNT(*) FILTER (WHERE reason LIKE '%%log%%' AND reason NOT LIKE '%%diff%%') as log_reject,
                        COUNT(*) FILTER (WHERE reason LIKE '%%diff%%' AND reason LIKE '%%log%%') as diff_log_reject,
                        COUNT(*) FILTER (WHERE reason LIKE '%%diff%%' OR reason LIKE '%%log%%') as total_intercept,
                        COUNT(*) FILTER (
                            WHERE (reason LIKE '%%diff%%' OR reason LIKE '%%log%%')
                            AND created_at > now() - interval '24 hours'
                        ) as recent_24h
                    FROM governance.write_audit
                    WHERE action = 'reject'
                """)
                row = cur.fetchone()
                
                if row:
                    return {
                        "diff_reject_count": row[0] or 0,
                        "log_reject_count": row[1] or 0,
                        "diff_log_reject_count": row[2] or 0,
                        "total_intercept_count": row[3] or 0,
                        "recent_24h_intercept": row[4] or 0,
                    }
            except Exception:
                pass
            
            # 查询失败时返回默认值
            return {
                "diff_reject_count": 0,
                "log_reject_count": 0,
                "diff_log_reject_count": 0,
                "total_intercept_count": 0,
                "recent_24h_intercept": 0,
            }

    # ======================== Logbook DB 检查与迁移 ========================

    def check_db_schema(self) -> LogbookDBCheckResult:
        """
        检查 Logbook 数据库的 schema/表/索引/物化视图是否存在
        
        使用 db_migrate.run_all_checks 进行检查。
        
        Returns:
            LogbookDBCheckResult 检查结果
        """
        if not _DB_MIGRATE_AVAILABLE:
            return LogbookDBCheckResult(
                ok=False,
                message="db_migrate 模块不可用，无法执行 DB 检查",
            )
        
        try:
            conn = get_connection(config=self._config)
            try:
                result = run_all_checks(conn)
                return LogbookDBCheckResult(
                    ok=result.get("ok", False),
                    checks=result.get("checks", {}),
                    message=None if result.get("ok") else "部分数据库结构缺失",
                )
            finally:
                conn.close()
        except Exception as e:
            return LogbookDBCheckResult(
                ok=False,
                message=f"DB 检查失败: {str(e)}",
            )

    def run_migration(self, quiet: bool = True) -> Dict[str, Any]:
        """
        执行 Logbook 数据库迁移
        
        使用 db_migrate.run_migrate 执行迁移。
        
        Args:
            quiet: 是否静默模式（减少输出）
            
        Returns:
            迁移结果字典 {ok: bool, code: str, ...}
        """
        if not _DB_MIGRATE_AVAILABLE:
            return {
                "ok": False,
                "code": LogbookDBErrorCode.MIGRATE_NOT_AVAILABLE,
                "message": "db_migrate 模块不可用，无法执行迁移",
            }
        
        try:
            result = run_migrate(
                dsn=self._dsn,
                quiet=quiet,
            )
            return result
        except Exception as e:
            return {
                "ok": False,
                "code": LogbookDBErrorCode.MIGRATE_FAILED,
                "message": f"迁移执行失败: {str(e)}",
            }

    def ensure_db_ready(
        self,
        auto_migrate: bool = False,
    ) -> LogbookDBCheckResult:
        """
        确保 Logbook DB 已就绪（schema/表/索引/物化视图存在）
        
        1. 检查 DB 结构是否完整
        2. 如果缺失且 auto_migrate=True，自动执行迁移
        3. 如果缺失且 auto_migrate=False，返回错误信息和修复指令
        
        Args:
            auto_migrate: 如果 DB 结构缺失，是否自动执行迁移
            
        Returns:
            LogbookDBCheckResult 检查结果
            
        Raises:
            LogbookDBCheckError: 如果 DB 结构缺失且无法自动修复
        """
        # 1. 执行检查
        check_result = self.check_db_schema()
        
        if check_result.ok:
            return check_result
        
        # 2. DB 结构缺失，确定错误码
        error_code = self._determine_error_code(check_result.checks)
        
        if auto_migrate:
            # 尝试自动迁移
            migrate_result = self.run_migration(quiet=True)
            
            if migrate_result.get("ok"):
                # 迁移成功，重新检查
                check_result = self.check_db_schema()
                if check_result.ok:
                    check_result.message = "DB 结构通过自动迁移修复"
                    return check_result
                else:
                    raise LogbookDBCheckError(
                        message=f"自动迁移后 DB 结构仍不完整: {check_result.get_missing_summary()}",
                        code=LogbookDBErrorCode.MIGRATE_PARTIAL,
                        missing_items=check_result.checks,
                    )
            else:
                raise LogbookDBCheckError(
                    message=f"自动迁移失败: {migrate_result.get('message', '未知错误')}",
                    code=LogbookDBErrorCode.MIGRATE_FAILED,
                    missing_items=check_result.checks,
                )
        else:
            # 不自动迁移，返回错误信息和修复指令
            repair_hint = (
                "请执行以下命令修复数据库结构:\n"
                "  cd apps/logbook_postgres/scripts\n"
                "  python db_migrate.py --dsn <your_postgres_dsn>\n"
                "或在项目根目录执行:\n"
                "  python apps/logbook_postgres/scripts/db_migrate.py --dsn <your_postgres_dsn>\n"
                "或设置环境变量 AUTO_MIGRATE_ON_STARTUP=true 启用自动迁移"
            )
            raise LogbookDBCheckError(
                message=f"Logbook DB 结构不完整: {check_result.get_missing_summary()}\n\n{repair_hint}",
                code=error_code,
                missing_items=check_result.checks,
            )
    
    def _determine_error_code(self, checks: Dict[str, Any]) -> str:
        """
        根据检查结果确定错误码
        
        优先级: schema > tables > indexes > matviews > 其他
        """
        if not checks:
            return LogbookDBErrorCode.STRUCTURE_INCOMPLETE
        
        # 按优先级检查
        priority_map = [
            ("schemas", LogbookDBErrorCode.SCHEMA_MISSING),
            ("tables", LogbookDBErrorCode.TABLE_MISSING),
            ("indexes", LogbookDBErrorCode.INDEX_MISSING),
            ("matviews", LogbookDBErrorCode.MATVIEW_MISSING),
        ]
        
        for check_name, error_code in priority_map:
            if check_name in checks:
                check_result = checks[check_name]
                if isinstance(check_result, dict) and not check_result.get("ok", True):
                    if check_result.get("missing"):
                        return error_code
        
        return LogbookDBErrorCode.STRUCTURE_INCOMPLETE


# 模块级别便捷函数
_adapter_instance: Optional[LogbookAdapter] = None


def get_adapter(dsn: Optional[str] = None) -> LogbookAdapter:
    """获取全局适配器实例"""
    global _adapter_instance
    if _adapter_instance is None:
        _adapter_instance = LogbookAdapter(dsn)
    return _adapter_instance


def reset_adapter() -> None:
    """重置全局适配器实例"""
    global _adapter_instance
    _adapter_instance = None


# ======================== Logbook Items 便捷函数 ========================

def create_item(
    item_type: str,
    title: str,
    scope_json: Optional[Dict] = None,
    status: str = "open",
    owner_user_id: Optional[str] = None,
) -> int:
    """
    在 logbook.items 中创建新条目
    
    Args:
        item_type: 条目类型（如 'evidence', 'note', 'task' 等）
        title: 标题
        scope_json: 范围元数据（默认 {}）
        status: 状态（默认 'open'）
        owner_user_id: 所有者用户 ID（可选）
        
    Returns:
        创建的 item_id
    """
    return get_adapter().create_item(
        item_type=item_type,
        title=title,
        scope_json=scope_json,
        status=status,
        owner_user_id=owner_user_id,
    )


# ======================== Outbox 便捷函数 ========================

def check_dedup(
    target_space: str,
    payload_sha: str,
) -> Optional[Dict[str, Any]]:
    """检查是否存在已成功写入的重复记录（幂等去重）"""
    return get_adapter().check_dedup(
        target_space=target_space,
        payload_sha=payload_sha,
    )


def claim_outbox(
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 60,
) -> List[Dict[str, Any]]:
    """并发安全地获取并锁定待处理的 outbox 记录"""
    return get_adapter().claim_outbox(
        worker_id=worker_id,
        limit=limit,
        lease_seconds=lease_seconds,
    )


def ack_sent(
    outbox_id: int,
    worker_id: str,
    memory_id: Optional[str] = None,
) -> bool:
    """确认 outbox 记录已成功发送"""
    return get_adapter().ack_sent(
        outbox_id=outbox_id,
        worker_id=worker_id,
        memory_id=memory_id,
    )


def fail_retry(
    outbox_id: int,
    worker_id: str,
    error: str,
    next_attempt_at: Any,
) -> bool:
    """标记 outbox 记录处理失败，安排重试（next_attempt_at 由调用方计算）"""
    return get_adapter().fail_retry(
        outbox_id=outbox_id,
        worker_id=worker_id,
        error=error,
        next_attempt_at=next_attempt_at,
    )


def mark_dead(
    outbox_id: int,
    worker_id: str,
    error: str,
) -> bool:
    """标记 outbox 记录为死信（带 worker_id 验证）"""
    return get_adapter().mark_dead_by_worker(
        outbox_id=outbox_id,
        worker_id=worker_id,
        error=error,
    )


def renew_lease(
    outbox_id: int,
    worker_id: str,
) -> bool:
    """
    续期 Lease 租约
    
    仅当 status='pending' 且 locked_by 匹配 worker_id 时才执行更新。
    """
    return get_adapter().renew_lease(
        outbox_id=outbox_id,
        worker_id=worker_id,
    )


def renew_lease_batch(
    outbox_ids: List[int],
    worker_id: str,
) -> int:
    """批量续期 Lease 租约"""
    return get_adapter().renew_lease_batch(
        outbox_ids=outbox_ids,
        worker_id=worker_id,
    )


def insert_write_audit(
    actor_user_id: Optional[str],
    target_space: str,
    action: str,
    reason: Optional[str] = None,
    payload_sha: Optional[str] = None,
    evidence_refs_json: Optional[Dict] = None,
    validate_refs: bool = False,
) -> int:
    """写入审计日志"""
    return get_adapter().insert_audit(
        actor_user_id=actor_user_id,
        target_space=target_space,
        action=action,
        reason=reason,
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
        validate_refs=validate_refs,
    )


def get_outbox_by_id(outbox_id: int) -> Optional[Dict[str, Any]]:
    """
    根据 outbox_id 获取单条记录
    
    Args:
        outbox_id: Outbox 记录 ID
        
    Returns:
        outbox 记录字典，不存在返回 None
    """
    return get_adapter().get_outbox_by_id(outbox_id=outbox_id)


# ======================== DB 检查便捷函数 ========================

def check_db_schema(dsn: Optional[str] = None) -> LogbookDBCheckResult:
    """
    检查 Logbook 数据库的 schema/表/索引/物化视图是否存在
    
    Args:
        dsn: PostgreSQL 连接字符串（可选）
        
    Returns:
        LogbookDBCheckResult 检查结果
    """
    return get_adapter(dsn).check_db_schema()


def ensure_db_ready(
    dsn: Optional[str] = None,
    auto_migrate: bool = False,
) -> LogbookDBCheckResult:
    """
    确保 Logbook DB 已就绪
    
    Args:
        dsn: PostgreSQL 连接字符串（可选）
        auto_migrate: 如果 DB 结构缺失，是否自动执行迁移
        
    Returns:
        LogbookDBCheckResult 检查结果
        
    Raises:
        LogbookDBCheckError: 如果 DB 结构缺失且无法自动修复
    """
    return get_adapter(dsn).ensure_db_ready(auto_migrate=auto_migrate)


def is_db_migrate_available() -> bool:
    """检查 db_migrate 模块是否可用"""
    return _DB_MIGRATE_AVAILABLE


# ======================== Analysis 查询便捷函数 ========================

def query_knowledge_candidates(
    keyword: str,
    top_k: int = 10,
    evidence_filter: Optional[str] = None,
    space_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    从 analysis.knowledge_candidates 表按关键词查询知识候选项
    
    用于 OpenMemory 查询失败时的降级回退。
    
    Args:
        keyword: 搜索关键词
        top_k: 返回结果数量上限（默认 10）
        evidence_filter: 可选，按 evidence_refs_json 过滤
        space_filter: 可选，按 space 过滤
        
    Returns:
        知识候选项列表
    """
    return get_adapter().query_knowledge_candidates(
        keyword=keyword,
        top_k=top_k,
        evidence_filter=evidence_filter,
        space_filter=space_filter,
    )


# ======================== 可靠性报告函数 ========================

def get_reliability_report() -> Dict[str, Any]:
    """
    获取可靠性统计报告
    
    聚合 logbook.outbox_memory 和 governance.write_audit 表的统计数据。
    
    Returns:
        可靠性报告字典，包含：
        - outbox_stats: outbox_memory 表统计
        - audit_stats: write_audit 表统计
        - generated_at: 报告生成时间 (ISO 8601)
    """
    return get_adapter().get_reliability_report()


# ======================== 用户管理便捷函数 ========================

def check_user_exists(user_id: str) -> bool:
    """
    检查用户是否存在
    
    Args:
        user_id: 用户标识
        
    Returns:
        True 如果用户存在
    """
    return get_adapter().check_user_exists(user_id)


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    """
    获取用户信息
    
    Args:
        user_id: 用户标识
        
    Returns:
        用户信息字典，不存在返回 None
    """
    return get_adapter().get_user(user_id)


def ensure_user(
    user_id: str,
    display_name: Optional[str] = None,
    is_active: bool = True,
    roles_json: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    确保用户存在（幂等 upsert）
    
    Args:
        user_id: 用户标识
        display_name: 显示名称（可选）
        is_active: 是否激活（默认 True）
        roles_json: 角色信息（默认 {}）
        
    Returns:
        用户信息字典
    """
    return get_adapter().ensure_user(
        user_id=user_id,
        display_name=display_name,
        is_active=is_active,
        roles_json=roles_json,
    )


def ensure_account(
    user_id: str,
    account_type: str,
    account_name: str,
    email: Optional[str] = None,
    aliases_json: Optional[List] = None,
    verified: bool = False,
) -> Dict[str, Any]:
    """
    确保账户存在（幂等 upsert）
    
    Args:
        user_id: 关联的用户标识
        account_type: 账户类型（svn/gitlab/git/email）
        account_name: 账户名称
        email: 邮箱地址（可选）
        aliases_json: 别名列表（默认 []）
        verified: 是否已验证（默认 False）
        
    Returns:
        账户信息字典
    """
    return get_adapter().ensure_account(
        user_id=user_id,
        account_type=account_type,
        account_name=account_name,
        email=email,
        aliases_json=aliases_json,
        verified=verified,
    )
