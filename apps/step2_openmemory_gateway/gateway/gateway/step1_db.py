"""
step1_db - Step1 数据库操作模块 (已弃用)

=== 模块状态与未来规划 ===

当前状态: 已弃用 (Deprecated)
未来状态: 作为 engram_step1 的适配层 (Adapter)

此模块的演进路径:
1. [已完成] 作为独立的 Step1 数据库操作实现
2. [当前] 作为向后兼容的代理层，转发调用到 step1_adapter
3. [未来] 完全移除，由 step1_adapter 直接复用 engram_step1 包

依赖关系:
- 本模块 → step1_adapter → engram_step1 (PyPI 包)
- engram_step1 提供 Step1 的 8 个核心原语接口:
  * get_or_create_settings, upsert_settings
  * insert_write_audit
  * outbox_enqueue, outbox_claim_lease, outbox_ack_sent
  * outbox_fail_retry, outbox_mark_dead

警告: 此模块已弃用，请使用 step1_adapter 模块代替。
新代码请直接使用:
    from gateway.step1_adapter import Step1Adapter, get_adapter
"""

import warnings
from datetime import datetime
from typing import Any, Dict, List, Optional

# 发出弃用警告
warnings.warn(
    "step1_db 模块已弃用，请使用 step1_adapter 模块代替。"
    "新代码请使用: from gateway.step1_adapter import Step1Adapter, get_adapter",
    DeprecationWarning,
    stacklevel=2,
)

# 尝试导入新适配器，如果失败则回退到旧实现
try:
    from .step1_adapter import Step1Adapter as _Step1Adapter
    from .step1_adapter import get_adapter as _get_adapter
    from .step1_adapter import reset_adapter as _reset_adapter
    _USE_ADAPTER = True
except ImportError:
    # 如果 engram_step1 包未安装，回退到旧实现
    _USE_ADAPTER = False
    _Step1Adapter = None
    _get_adapter = None
    _reset_adapter = None


class Step1Database:
    """
    Step1 数据库连接管理器 (已弃用)
    
    警告: 此类已弃用，请使用 step1_adapter.Step1Adapter 代替。
    """

    def __init__(self, dsn: Optional[str] = None):
        """
        初始化数据库连接
        
        Args:
            dsn: PostgreSQL 连接字符串，为 None 时从环境变量读取
        """
        if _USE_ADAPTER:
            self._adapter = _Step1Adapter(dsn=dsn)
        else:
            # 回退到旧实现
            import hashlib
            import json
            import os
            from datetime import timedelta

            import psycopg

            # DSN 优先级: 显式参数 > POSTGRES_DSN > TEST_PG_DSN
            self._dsn = dsn or os.environ.get("POSTGRES_DSN") or os.environ.get("TEST_PG_DSN", "")
            if not self._dsn:
                raise ValueError("需要设置 POSTGRES_DSN 或 TEST_PG_DSN 环境变量或传入 dsn 参数")
            self._adapter = None
            self._hashlib = hashlib
            self._json = json
            self._timedelta = timedelta
            self._psycopg = psycopg

    def _get_connection(self, autocommit: bool = False):
        """获取数据库连接"""
        if self._adapter:
            raise NotImplementedError("使用适配器时不应调用此方法")
        return self._psycopg.connect(self._dsn, autocommit=autocommit)

    # ======================== governance.settings ========================

    def get_settings(self, project_key: str) -> Optional[Dict[str, Any]]:
        """
        读取治理设置
        
        Args:
            project_key: 项目标识
            
        Returns:
            设置字典 {team_write_enabled, policy_json, updated_by, updated_at}
            如果不存在返回 None
        """
        if self._adapter:
            return self._adapter.get_settings(project_key)
        
        # 回退实现
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT team_write_enabled, policy_json, updated_by, updated_at
                    FROM governance.settings
                    WHERE project_key = %s
                    """,
                    (project_key,),
                )
                row = cur.fetchone()
                if row:
                    return {
                        "project_key": project_key,
                        "team_write_enabled": row[0],
                        "policy_json": row[1],
                        "updated_by": row[2],
                        "updated_at": row[3],
                    }
                return None
        finally:
            conn.close()

    def get_or_create_settings(self, project_key: str) -> Dict[str, Any]:
        """
        获取或创建治理设置（默认 team_write_enabled=false）
        
        Args:
            project_key: 项目标识
            
        Returns:
            设置字典
        """
        if self._adapter:
            return self._adapter.get_or_create_settings(project_key)
        
        # 回退实现
        settings = self.get_settings(project_key)
        if settings:
            return settings

        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO governance.settings (project_key, team_write_enabled, policy_json)
                    VALUES (%s, false, '{}')
                    ON CONFLICT (project_key) DO NOTHING
                    RETURNING team_write_enabled, policy_json, updated_by, updated_at
                    """,
                    (project_key,),
                )
                result = cur.fetchone()
                conn.commit()

                if result:
                    return {
                        "project_key": project_key,
                        "team_write_enabled": result[0],
                        "policy_json": result[1],
                        "updated_by": result[2],
                        "updated_at": result[3],
                    }
                return self.get_settings(project_key) or {
                    "project_key": project_key,
                    "team_write_enabled": False,
                    "policy_json": {},
                    "updated_by": None,
                    "updated_at": None,
                }
        finally:
            conn.close()

    # ======================== governance.write_audit ========================

    def insert_audit(
        self,
        actor_user_id: Optional[str],
        target_space: str,
        action: str,
        reason: Optional[str] = None,
        payload_sha: Optional[str] = None,
        evidence_refs_json: Optional[Dict] = None,
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
            
        Returns:
            创建的 audit_id
        """
        if self._adapter:
            return self._adapter.insert_audit(
                actor_user_id=actor_user_id,
                target_space=target_space,
                action=action,
                reason=reason,
                payload_sha=payload_sha,
                evidence_refs_json=evidence_refs_json,
            )
        
        # 回退实现
        evidence_refs = evidence_refs_json or {}

        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO governance.write_audit
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
                        self._json.dumps(evidence_refs),
                    ),
                )
                result = cur.fetchone()
                conn.commit()
                return result[0]
        except self._psycopg.Error as e:
            conn.rollback()
            raise RuntimeError(f"写入审计日志失败: {e}")
        finally:
            conn.close()

    # ======================== logbook.outbox_memory ========================

    def enqueue_outbox(
        self,
        payload_md: str,
        target_space: str,
        item_id: Optional[int] = None,
        last_error: Optional[str] = None,
        next_attempt_at: Optional[datetime] = None,
    ) -> int:
        """
        将记忆入队到 outbox_memory 表（失败补偿队列）
        
        Args:
            payload_md: Markdown 格式的记忆内容
            target_space: 目标空间
            item_id: 关联的 logbook.items.item_id（可选）
            last_error: 错误信息
            next_attempt_at: 下次重试时间（适配器模式下忽略此参数）
            
        Returns:
            创建的 outbox_id
        """
        if self._adapter:
            return self._adapter.enqueue_outbox(
                payload_md=payload_md,
                target_space=target_space,
                item_id=item_id,
                last_error=last_error,
            )
        
        # 回退实现
        payload_sha = self._hashlib.sha256(payload_md.encode("utf-8")).hexdigest()

        if next_attempt_at is None:
            next_attempt_at = datetime.utcnow() + self._timedelta(minutes=5)

        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO logbook.outbox_memory
                        (item_id, target_space, payload_md, payload_sha, status, 
                         retry_count, last_error, next_attempt_at)
                    VALUES (%s, %s, %s, %s, 'pending', 0, %s, %s)
                    RETURNING outbox_id
                    """,
                    (item_id, target_space, payload_md, payload_sha, last_error, next_attempt_at),
                )
                result = cur.fetchone()
                conn.commit()
                return result[0]
        except self._psycopg.Error as e:
            conn.rollback()
            raise RuntimeError(f"入队 outbox_memory 失败: {e}")
        finally:
            conn.close()

    def get_pending_outbox(
        self,
        limit: int = 100,
        before_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取待处理的 outbox 记录
        
        Args:
            limit: 返回记录数量上限
            before_time: 只返回 next_attempt_at <= before_time 的记录（适配器模式下忽略）
            
        Returns:
            pending 状态的 outbox 记录列表
        """
        if self._adapter:
            return self._adapter.get_pending_outbox(limit=limit)
        
        # 回退实现
        if before_time is None:
            before_time = datetime.utcnow()

        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT outbox_id, item_id, target_space, payload_md, payload_sha,
                           status, retry_count, last_error, next_attempt_at, created_at, updated_at
                    FROM logbook.outbox_memory
                    WHERE status = 'pending' AND next_attempt_at <= %s
                    ORDER BY next_attempt_at ASC
                    LIMIT %s
                    """,
                    (before_time, limit),
                )
                rows = cur.fetchall()
                return [
                    {
                        "outbox_id": row[0],
                        "item_id": row[1],
                        "target_space": row[2],
                        "payload_md": row[3],
                        "payload_sha": row[4],
                        "status": row[5],
                        "retry_count": row[6],
                        "last_error": row[7],
                        "next_attempt_at": row[8],
                        "created_at": row[9],
                        "updated_at": row[10],
                    }
                    for row in rows
                ]
        finally:
            conn.close()

    def mark_outbox_sent(self, outbox_id: int) -> bool:
        """
        标记 outbox 记录为已发送 (pending -> sent)
        
        Args:
            outbox_id: Outbox 记录 ID
            
        Returns:
            True 表示成功更新
        """
        if self._adapter:
            return self._adapter.mark_outbox_sent(outbox_id)
        
        # 回退实现
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE logbook.outbox_memory
                    SET status = 'sent', updated_at = now()
                    WHERE outbox_id = %s AND status = 'pending'
                    RETURNING outbox_id
                    """,
                    (outbox_id,),
                )
                result = cur.fetchone()
                conn.commit()
                return result is not None
        except self._psycopg.Error as e:
            conn.rollback()
            raise RuntimeError(f"标记 outbox 为 sent 失败: {e}")
        finally:
            conn.close()

    def increment_outbox_retry(
        self,
        outbox_id: int,
        error: str,
        next_attempt_at: Optional[datetime] = None,
    ) -> int:
        """
        增加 outbox 重试计数并更新错误信息
        
        Args:
            outbox_id: Outbox 记录 ID
            error: 本次错误信息
            next_attempt_at: 下次重试时间（适配器模式下忽略，使用指数退避）
            
        Returns:
            更新后的 retry_count
        """
        if self._adapter:
            return self._adapter.increment_outbox_retry(outbox_id=outbox_id, error=error)
        
        # 回退实现
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT retry_count FROM logbook.outbox_memory WHERE outbox_id = %s",
                    (outbox_id,),
                )
                row = cur.fetchone()
                if not row:
                    return 0

                new_retry_count = row[0] + 1
                if next_attempt_at is None:
                    next_attempt_at = datetime.utcnow() + self._timedelta(minutes=5 * (2 ** row[0]))

                cur.execute(
                    """
                    UPDATE logbook.outbox_memory
                    SET retry_count = %s, last_error = %s, next_attempt_at = %s, updated_at = now()
                    WHERE outbox_id = %s
                    RETURNING retry_count
                    """,
                    (new_retry_count, error, next_attempt_at, outbox_id),
                )
                result = cur.fetchone()
                conn.commit()
                return result[0] if result else 0
        except self._psycopg.Error as e:
            conn.rollback()
            raise RuntimeError(f"增加 outbox retry_count 失败: {e}")
        finally:
            conn.close()

    def mark_outbox_dead(self, outbox_id: int, error: str) -> bool:
        """
        标记 outbox 记录为死信 (pending -> dead)
        
        Args:
            outbox_id: Outbox 记录 ID
            error: 错误信息
            
        Returns:
            True 表示成功更新
        """
        if self._adapter:
            return self._adapter.mark_outbox_dead(outbox_id=outbox_id, error=error)
        
        # 回退实现
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE logbook.outbox_memory
                    SET status = 'dead', last_error = %s, updated_at = now()
                    WHERE outbox_id = %s AND status = 'pending'
                    RETURNING outbox_id
                    """,
                    (error, outbox_id),
                )
                result = cur.fetchone()
                conn.commit()
                return result is not None
        except self._psycopg.Error as e:
            conn.rollback()
            raise RuntimeError(f"标记 outbox 为 dead 失败: {e}")
        finally:
            conn.close()


# 模块级别便捷函数
# 使用 DSN 作为 key 的字典缓存，支持不同 DSN 使用不同实例
_db_instances: Dict[str, Step1Database] = {}
# 默认 DSN（用于无参数调用时）
_default_dsn: Optional[str] = None


def get_db(dsn: Optional[str] = None) -> Step1Database:
    """
    获取全局数据库实例 (已弃用)
    
    警告: 此函数已弃用，请使用 step1_adapter.get_adapter() 代替。
    
    Args:
        dsn: PostgreSQL 连接字符串。不同 DSN 会创建不同实例。
             如果为 None，使用默认 DSN（从环境变量 POSTGRES_DSN 读取）。
    
    Returns:
        Step1Database 实例
    
    Note:
        不同 DSN 不会复用同一实例，以支持测试场景中切换数据库。
    """
    global _db_instances, _default_dsn
    import os
    
    # 确定实际使用的 DSN
    effective_dsn = dsn or _default_dsn or os.environ.get("POSTGRES_DSN", "")
    
    # 使用 DSN 作为 key 缓存实例
    if effective_dsn not in _db_instances:
        _db_instances[effective_dsn] = Step1Database(dsn=dsn)
        # 如果是首次调用且没有指定默认 DSN，设置为默认
        if _default_dsn is None and dsn:
            _default_dsn = dsn
    
    return _db_instances[effective_dsn]


def set_default_dsn(dsn: str) -> None:
    """
    设置默认 DSN
    
    Args:
        dsn: PostgreSQL 连接字符串
    """
    global _default_dsn
    _default_dsn = dsn


def reset_db(dsn: Optional[str] = None) -> None:
    """
    重置全局数据库实例 (已弃用)
    
    警告: 此函数已弃用，请使用 step1_adapter.reset_adapter() 代替。
    
    Args:
        dsn: 要重置的特定 DSN。如果为 None，重置所有实例。
    """
    global _db_instances, _default_dsn
    
    if dsn is None:
        # 重置所有实例
        _db_instances.clear()
        _default_dsn = None
    else:
        # 只重置指定 DSN 的实例
        _db_instances.pop(dsn, None)
        if _default_dsn == dsn:
            _default_dsn = None
    
    if _USE_ADAPTER and _reset_adapter:
        _reset_adapter()
