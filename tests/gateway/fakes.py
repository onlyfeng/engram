# -*- coding: utf-8 -*-
"""
Gateway 测试用 Fake 依赖

提供可注入的 Fake 对象，用于替代外部依赖（OpenMemory、Logbook DB）。
支持配置不同的响应行为和失败模式。

================================================================================
依赖注入使用方式 (v1.0):
================================================================================

所有测试应通过 GatewayDeps.for_testing() 注入依赖，而非使用旧的 _openmemory_client 参数。

使用示例:
    from engram.gateway.di import GatewayDeps
    from tests.gateway.fakes import (
        FakeGatewayConfig,
        FakeLogbookAdapter,
        FakeLogbookDatabase,
        FakeOpenMemoryClient,
    )

    # 创建 fake 依赖
    fake_config = FakeGatewayConfig()
    fake_db = FakeLogbookDatabase()
    fake_adapter = FakeLogbookAdapter()
    fake_client = FakeOpenMemoryClient()

    # 配置 fake 行为
    fake_client.configure_store_success(memory_id="mem_123")
    # 或配置为失败模式
    fake_client.configure_store_connection_error("连接超时")

    # 通过 GatewayDeps.for_testing() 注入依赖
    deps = GatewayDeps.for_testing(
        config=fake_config,
        db=fake_db,
        logbook_adapter=fake_adapter,
        openmemory_client=fake_client,
    )

    # 调用被测函数
    result = await memory_store_impl(
        payload_md="test",
        correlation_id="corr-0e50123456789000",
        deps=deps,
    )

注意:
- deps 参数是必需的 keyword-only 参数
- correlation_id 也是必需参数，必须由调用方提供
- 不再支持旧的 _config/_openmemory_client 参数
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# ============== OpenMemory Fake Client ==============
# 导入真实的 OpenMemory 异常类型，让 fake 异常继承自它们
# 这样 memory_store_impl 中的异常处理可以正确捕获 fake 异常
from engram.gateway.openmemory_client import (
    OpenMemoryAPIError,
    OpenMemoryConnectionError,
    OpenMemoryError,
)


@dataclass
class FakeStoreResult:
    """Fake 存储结果"""

    success: bool
    memory_id: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class FakeSearchResult:
    """Fake 搜索结果"""

    success: bool
    results: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


class FakeOpenMemoryConnectionError(OpenMemoryConnectionError):
    """
    Fake OpenMemory 连接异常

    继承自真实的 OpenMemoryConnectionError，确保被 memory_store_impl 正确捕获。
    """

    pass


class FakeOpenMemoryAPIError(OpenMemoryAPIError):
    """
    Fake OpenMemory API 异常

    继承自真实的 OpenMemoryAPIError，确保被 memory_store_impl 正确捕获。
    """

    pass


class FakeOpenMemoryError(OpenMemoryError):
    """
    Fake OpenMemory 通用异常

    继承自真实的 OpenMemoryError，确保被 memory_store_impl 正确捕获。
    """

    pass


class FakeOpenMemoryClient:
    """
    可配置的 Fake OpenMemory 客户端

    支持配置:
    - 成功响应
    - 连接失败
    - API 错误 (4xx/5xx)
    - 通用错误
    - 自定义回调
    """

    def __init__(
        self,
        base_url: str = "http://fake-openmemory:8080",
        api_key: Optional[str] = "fake_api_key",
    ):
        self.base_url = base_url
        self.api_key = api_key

        # 存储调用记录
        self.store_calls: List[Dict[str, Any]] = []
        self.search_calls: List[Dict[str, Any]] = []

        # 配置的响应行为
        self._store_behavior: Optional[Callable] = None
        self._search_behavior: Optional[Callable] = None

        # 默认配置为成功响应
        self.configure_store_success()
        self.configure_search_success()

    # ============== 配置方法 ==============

    def configure_store_success(
        self,
        memory_id: str = "fake_memory_id",
        data: Optional[Dict[str, Any]] = None,
    ):
        """配置 store 返回成功"""

        def _behavior(**kwargs):
            return FakeStoreResult(
                success=True,
                memory_id=memory_id,
                data=data or {"id": memory_id},
            )

        self._store_behavior = _behavior

    def configure_store_failure(self, error: str = "存储失败"):
        """配置 store 返回失败（success=False）"""

        def _behavior(**kwargs):
            return FakeStoreResult(
                success=False,
                memory_id=None,
                error=error,
            )

        self._store_behavior = _behavior

    def configure_store_connection_error(self, message: str = "连接超时"):
        """配置 store 抛出连接异常"""

        def _behavior(**kwargs):
            raise FakeOpenMemoryConnectionError(message=message)

        self._store_behavior = _behavior

    def configure_store_api_error(
        self,
        message: str = "API 错误",
        status_code: int = 500,
        response: Optional[Dict] = None,
    ):
        """配置 store 抛出 API 异常"""

        def _behavior(**kwargs):
            raise FakeOpenMemoryAPIError(
                message=message,
                status_code=status_code,
                response=response or {"error": message},
            )

        self._store_behavior = _behavior

    def configure_store_generic_error(self, message: str = "未知错误"):
        """配置 store 抛出通用异常"""

        def _behavior(**kwargs):
            raise FakeOpenMemoryError(message=message)

        self._store_behavior = _behavior

    def configure_store_callback(self, callback: Callable):
        """配置 store 使用自定义回调"""
        self._store_behavior = callback

    def configure_search_success(self, results: Optional[List[Dict[str, Any]]] = None):
        """配置 search 返回成功"""

        def _behavior(**kwargs):
            return FakeSearchResult(
                success=True,
                results=results or [],
            )

        self._search_behavior = _behavior

    def configure_search_failure(self, error: str = "搜索失败"):
        """配置 search 返回失败"""

        def _behavior(**kwargs):
            return FakeSearchResult(
                success=False,
                error=error,
            )

        self._search_behavior = _behavior

    def configure_search_connection_error(self, message: str = "连接超时"):
        """配置 search 抛出连接异常"""

        def _behavior(**kwargs):
            raise FakeOpenMemoryConnectionError(message=message)

        self._search_behavior = _behavior

    def configure_search_api_error(
        self,
        message: str = "API 错误",
        status_code: int = 500,
        response: Optional[Dict] = None,
    ):
        """配置 search 抛出 API 异常"""

        def _behavior(**kwargs):
            raise FakeOpenMemoryAPIError(
                message=message,
                status_code=status_code,
                response=response or {"error": message},
            )

        self._search_behavior = _behavior

    def configure_search_callback(self, callback: Callable):
        """配置 search 使用自定义回调"""
        self._search_behavior = callback

    # ============== API 方法 ==============

    def store(
        self,
        content: str,
        space: Optional[str] = None,
        user_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> FakeStoreResult:
        """存储记忆（模拟）"""
        call_args = {
            "content": content,
            "space": space,
            "user_id": user_id,
            "tags": tags,
            "metadata": metadata or meta,
        }
        self.store_calls.append(call_args)

        return self._store_behavior(**call_args)

    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> FakeSearchResult:
        """搜索记忆（模拟）"""
        call_args = {
            "query": query,
            "user_id": user_id,
            "limit": limit,
            "filters": filters,
        }
        self.search_calls.append(call_args)

        return self._search_behavior(**call_args)

    def health_check(self) -> bool:
        """健康检查（默认返回 True）"""
        return True

    # ============== 辅助方法 ==============

    def reset_calls(self):
        """重置调用记录"""
        self.store_calls.clear()
        self.search_calls.clear()

    def get_last_store_call(self) -> Optional[Dict[str, Any]]:
        """获取最后一次 store 调用"""
        return self.store_calls[-1] if self.store_calls else None

    def get_last_search_call(self) -> Optional[Dict[str, Any]]:
        """获取最后一次 search 调用"""
        return self.search_calls[-1] if self.search_calls else None


# ============== Logbook Database Fake ==============


class FakeLogbookDatabase:
    """
    可配置的 Fake Logbook 数据库

    支持配置:
    - settings 响应
    - audit 写入行为（成功/失败）
    - outbox 入队行为
    - dedup check 响应
    """

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or "postgresql://fake:fake@localhost/fakedb"

        # 存储调用记录
        self.audit_calls: List[Dict[str, Any]] = []
        self.outbox_calls: List[Dict[str, Any]] = []
        self.settings_calls: List[Dict[str, Any]] = []

        # 配置的响应
        self._settings: Dict[str, Any] = {
            "team_write_enabled": False,
            "policy_json": {},
        }
        self._next_audit_id: int = 1
        self._next_outbox_id: int = 1

        # 失败模式
        self._audit_should_fail: bool = False
        self._audit_fail_error: str = "审计写入失败"
        self._outbox_should_fail: bool = False
        self._outbox_fail_error: str = "入队失败"

    # ============== 配置方法 ==============

    def configure_settings(
        self,
        team_write_enabled: bool = False,
        policy_json: Optional[Dict[str, Any]] = None,
        **extra_settings,
    ):
        """配置 settings 响应"""
        self._settings = {
            "team_write_enabled": team_write_enabled,
            "policy_json": policy_json or {},
            **extra_settings,
        }

    def configure_audit_success(self, start_id: int = 1):
        """配置 audit 写入成功"""
        self._audit_should_fail = False
        self._next_audit_id = start_id

    def configure_audit_failure(self, error: str = "审计写入失败"):
        """配置 audit 写入失败"""
        self._audit_should_fail = True
        self._audit_fail_error = error

    def configure_outbox_success(self, start_id: int = 1):
        """配置 outbox 入队成功"""
        self._outbox_should_fail = False
        self._next_outbox_id = start_id

    def configure_outbox_failure(self, error: str = "入队失败"):
        """配置 outbox 入队失败"""
        self._outbox_should_fail = True
        self._outbox_fail_error = error

    # ============== API 方法 ==============

    def get_settings(self, project_key: str) -> Optional[Dict[str, Any]]:
        """读取治理设置"""
        self.settings_calls.append({"action": "get", "project_key": project_key})
        return self._settings.copy()

    def get_or_create_settings(self, project_key: str) -> Dict[str, Any]:
        """获取或创建治理设置"""
        self.settings_calls.append({"action": "get_or_create", "project_key": project_key})
        return self._settings.copy()

    def upsert_settings(
        self,
        project_key: str,
        team_write_enabled: Optional[bool] = None,
        policy_json: Optional[Dict[str, Any]] = None,
        updated_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """更新治理设置"""
        self.settings_calls.append(
            {
                "action": "upsert",
                "project_key": project_key,
                "team_write_enabled": team_write_enabled,
                "policy_json": policy_json,
                "updated_by": updated_by,
            }
        )
        if team_write_enabled is not None:
            self._settings["team_write_enabled"] = team_write_enabled
        if policy_json is not None:
            self._settings["policy_json"] = policy_json
        return self._settings.copy()

    def insert_audit(
        self,
        actor_user_id: Optional[str],
        target_space: str,
        action: str,
        reason: Optional[str] = None,
        payload_sha: Optional[str] = None,
        evidence_refs_json: Optional[Dict] = None,
        validate_refs: bool = False,
        correlation_id: Optional[str] = None,
        status: str = "success",
    ) -> int:
        """写入审计日志"""
        call_record = {
            "actor_user_id": actor_user_id,
            "target_space": target_space,
            "action": action,
            "reason": reason,
            "payload_sha": payload_sha,
            "evidence_refs_json": evidence_refs_json,
            "validate_refs": validate_refs,
            "correlation_id": correlation_id,
            "status": status,
        }
        self.audit_calls.append(call_record)

        if self._audit_should_fail:
            raise RuntimeError(self._audit_fail_error)

        audit_id = self._next_audit_id
        self._next_audit_id += 1
        return audit_id

    def enqueue_outbox(
        self,
        payload_md: str,
        target_space: str,
        item_id: Optional[int] = None,
        last_error: Optional[str] = None,
        next_attempt_at: Optional[datetime] = None,
    ) -> int:
        """将记忆入队到 outbox_memory 表"""
        call_record = {
            "payload_md": payload_md,
            "target_space": target_space,
            "item_id": item_id,
            "last_error": last_error,
        }
        self.outbox_calls.append(call_record)

        if self._outbox_should_fail:
            raise RuntimeError(self._outbox_fail_error)

        outbox_id = self._next_outbox_id
        self._next_outbox_id += 1
        return outbox_id

    def get_pending_outbox(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取待处理的 outbox 记录"""
        # 返回记录的 outbox 入队
        return [
            {
                "outbox_id": i + 1,
                "payload_md": call["payload_md"],
                "target_space": call["target_space"],
                "payload_sha": "fake_sha",
                "status": "pending",
                "retry_count": 0,
            }
            for i, call in enumerate(self.outbox_calls[:limit])
        ]

    def mark_outbox_sent(self, outbox_id: int) -> bool:
        """标记 outbox 记录为已发送"""
        return True

    def increment_outbox_retry(self, outbox_id: int, error: str) -> int:
        """增加 outbox 重试计数"""
        return 1

    def mark_outbox_dead(self, outbox_id: int, error: str) -> bool:
        """标记 outbox 记录为死信"""
        return True

    # ============== 辅助方法 ==============

    def reset_calls(self):
        """重置调用记录"""
        self.audit_calls.clear()
        self.outbox_calls.clear()
        self.settings_calls.clear()

    def get_audit_calls(self) -> List[Dict[str, Any]]:
        """获取所有 audit 调用记录"""
        return self.audit_calls.copy()

    def get_last_audit_call(self) -> Optional[Dict[str, Any]]:
        """获取最后一次 audit 调用"""
        return self.audit_calls[-1] if self.audit_calls else None

    def get_outbox_calls(self) -> List[Dict[str, Any]]:
        """获取所有 outbox 调用记录"""
        return self.outbox_calls.copy()

    def get_last_outbox_call(self) -> Optional[Dict[str, Any]]:
        """获取最后一次 outbox 调用"""
        return self.outbox_calls[-1] if self.outbox_calls else None


# ============== Fake logbook_adapter 模块 ==============


class FakeLogbookAdapter:
    """
    Fake logbook_adapter 模块，模拟 check_dedup、query_knowledge_candidates、
    check_user_exists、ensure_user、write_audit、update_write_audit 等方法
    """

    def __init__(self):
        self._dedup_result: Optional[Dict[str, Any]] = None
        self._knowledge_candidates: List[Dict[str, Any]] = []
        self._user_exists: bool = True  # 默认用户存在

        # 配置的 settings
        self._settings: Dict[str, Any] = {
            "team_write_enabled": False,
            "policy_json": {},
        }

        # 调用记录
        self.dedup_calls: List[Dict[str, Any]] = []
        self.query_calls: List[Dict[str, Any]] = []
        self.check_user_calls: List[str] = []
        self.ensure_user_calls: List[Dict[str, Any]] = []

        # 审计调用记录（两阶段审计支持）
        self._audit_calls: List[Dict[str, Any]] = []
        self._update_audit_calls: List[Dict[str, Any]] = []
        self._outbox_calls: List[Dict[str, Any]] = []
        self._audit_records: Dict[str, Dict[str, Any]] = {}  # correlation_id -> record
        self._next_audit_id: int = 1
        self._next_outbox_id: int = 1

    def configure_dedup_hit(
        self,
        outbox_id: int = 100,
        target_space: str = "team:test",
        payload_sha: str = "fake_sha",
        status: str = "sent",
        memory_id: Optional[str] = "mem_existing",
    ):
        """配置 dedup 命中"""
        self._dedup_result = {
            "outbox_id": outbox_id,
            "target_space": target_space,
            "payload_sha": payload_sha,
            "status": status,
            "last_error": f"memory_id={memory_id}" if memory_id else None,
        }

    def configure_dedup_miss(self):
        """配置 dedup 未命中"""
        self._dedup_result = None

    def configure_knowledge_candidates(self, candidates: List[Dict[str, Any]]):
        """配置 knowledge_candidates 查询结果"""
        self._knowledge_candidates = candidates

    def configure_user_exists(self, exists: bool = True):
        """配置 check_user_exists 返回值"""
        self._user_exists = exists

    def configure_outbox_success(self, start_id: int = 1):
        """配置 outbox 入队成功，设置起始 ID"""
        self._next_outbox_id = start_id

    def configure_settings(
        self,
        team_write_enabled: bool = False,
        policy_json: Optional[Dict[str, Any]] = None,
        **extra_settings,
    ):
        """配置 settings 响应"""
        self._settings = {
            "team_write_enabled": team_write_enabled,
            "policy_json": policy_json or {},
            **extra_settings,
        }

    def check_user_exists(self, user_id: str) -> bool:
        """检查用户是否存在"""
        self.check_user_calls.append(user_id)
        return self._user_exists

    def ensure_user(
        self,
        user_id: str,
        display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """确保用户存在（自动创建）"""
        self.ensure_user_calls.append(
            {
                "user_id": user_id,
                "display_name": display_name,
            }
        )
        return {
            "user_id": user_id,
            "display_name": display_name or user_id,
        }

    def check_dedup(
        self,
        target_space: str,
        payload_sha: str,
    ) -> Optional[Dict[str, Any]]:
        """检查 dedup"""
        self.dedup_calls.append(
            {
                "target_space": target_space,
                "payload_sha": payload_sha,
            }
        )
        return self._dedup_result

    def query_knowledge_candidates(
        self,
        keyword: str,
        top_k: int = 10,
        evidence_filter: Optional[str] = None,
        space_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询 knowledge_candidates（用于 fallback）"""
        self.query_calls.append(
            {
                "keyword": keyword,
                "top_k": top_k,
                "evidence_filter": evidence_filter,
                "space_filter": space_filter,
            }
        )
        return self._knowledge_candidates

    def upsert_settings(
        self,
        project_key: str,
        team_write_enabled: Optional[bool] = None,
        policy_json: Optional[Dict[str, Any]] = None,
        updated_by: Optional[str] = None,
    ) -> bool:
        """
        更新治理设置（用于 governance_update handler）

        Returns:
            bool: 成功返回 True
        """
        # 此方法为 governance_update_impl 所需
        return True

    def get_settings(self, project_key: str) -> Optional[Dict[str, Any]]:
        """读取治理设置"""
        return self._settings.copy()

    def get_or_create_settings(self, project_key: str) -> Dict[str, Any]:
        """获取或创建治理设置"""
        return self._settings.copy()

    # ============== 两阶段审计支持 ==============

    def write_audit(
        self,
        correlation_id: str,
        actor_user_id: Optional[str] = None,
        target_space: Optional[str] = None,
        action: str = "store",
        reason: Optional[str] = None,
        payload_sha: Optional[str] = None,
        evidence_refs_json: Optional[Dict] = None,
        status: str = "pending",
        **kwargs,
    ) -> int:
        """写入审计日志（两阶段审计 phase-1）"""
        call_record = {
            "correlation_id": correlation_id,
            "actor_user_id": actor_user_id,
            "target_space": target_space,
            "action": action,
            "reason": reason,
            "payload_sha": payload_sha,
            "evidence_refs_json": evidence_refs_json or {},
            "status": status,
        }
        self._audit_calls.append(call_record)

        # 存储审计记录
        audit_id = self._next_audit_id
        self._next_audit_id += 1
        self._audit_records[correlation_id] = {
            "audit_id": audit_id,
            **call_record,
        }
        return audit_id

    def update_write_audit(
        self,
        correlation_id: str,
        status: str,
        reason_suffix: Optional[str] = None,
        evidence_refs_json_patch: Optional[Dict] = None,
        **kwargs,
    ) -> bool:
        """更新审计日志（两阶段审计 phase-2 finalize）"""
        call_record = {
            "correlation_id": correlation_id,
            "status": status,
            "reason_suffix": reason_suffix,
            "evidence_refs_json_patch": evidence_refs_json_patch or {},
        }
        self._update_audit_calls.append(call_record)

        # 更新审计记录
        if correlation_id in self._audit_records:
            self._audit_records[correlation_id]["status"] = status
            if reason_suffix:
                old_reason = self._audit_records[correlation_id].get("reason", "") or ""
                self._audit_records[correlation_id]["reason"] = (
                    f"{old_reason} {reason_suffix}".strip()
                )
            if evidence_refs_json_patch:
                old_refs = self._audit_records[correlation_id].get("evidence_refs_json", {})
                self._audit_records[correlation_id]["evidence_refs_json"] = {
                    **old_refs,
                    **evidence_refs_json_patch,
                }
        return True

    def enqueue_outbox(
        self,
        payload_md: str,
        target_space: str,
        correlation_id: Optional[str] = None,
        item_id: Optional[int] = None,
        last_error: Optional[str] = None,
        next_attempt_at: Optional[datetime] = None,
        **kwargs,
    ) -> int:
        """将记忆入队到 outbox_memory 表"""
        call_record = {
            "payload_md": payload_md,
            "target_space": target_space,
            "correlation_id": correlation_id,
            "item_id": item_id,
            "last_error": last_error,
        }
        self._outbox_calls.append(call_record)

        outbox_id = self._next_outbox_id
        self._next_outbox_id += 1
        return outbox_id

    def get_audit_calls(self) -> List[Dict[str, Any]]:
        """获取所有 write_audit 调用记录"""
        return self._audit_calls.copy()

    def get_update_audit_calls(self) -> List[Dict[str, Any]]:
        """获取所有 update_write_audit 调用记录"""
        return self._update_audit_calls.copy()

    def get_outbox_calls(self) -> List[Dict[str, Any]]:
        """获取所有 enqueue_outbox 调用记录"""
        return self._outbox_calls.copy()

    def get_audit_record_by_correlation_id(self, correlation_id: str) -> Optional[Dict[str, Any]]:
        """通过 correlation_id 查询审计记录"""
        return self._audit_records.get(correlation_id)

    def reset_calls(self):
        """重置调用记录"""
        self.dedup_calls.clear()
        self.query_calls.clear()
        self.check_user_calls.clear()
        self.ensure_user_calls.clear()
        self._audit_calls.clear()
        self._update_audit_calls.clear()
        self._outbox_calls.clear()
        self._audit_records.clear()


# ============== Fake GatewayConfig ==============


@dataclass
class FakeGatewayConfig:
    """Fake Gateway 配置"""

    project_key: str = "test_project"
    postgres_dsn: str = "postgresql://fake:fake@localhost/fakedb"
    default_team_space: str = "team:test_project"
    private_space_prefix: str = "private:"
    openmemory_base_url: str = "http://fake-openmemory:8080"
    openmemory_api_key: Optional[str] = "fake_api_key"
    governance_admin_key: Optional[str] = None
    unknown_actor_policy: str = "degrade"
    logbook_check_on_startup: bool = False
    auto_migrate_on_startup: bool = False
    gateway_port: int = 8787
    # 新增必需字段
    minio_audit_webhook_auth_token: Optional[str] = None
    minio_audit_max_payload_size: int = 1024 * 1024
    validate_evidence_refs: bool = False
    strict_mode_enforce_validate_refs: bool = True


# ============== 集成工厂方法 ==============


def create_test_dependencies(
    team_write_enabled: bool = False,
    policy_json: Optional[Dict[str, Any]] = None,
    dedup_hit: bool = False,
    openmemory_success: bool = True,
    openmemory_error_type: Optional[str] = None,  # "connection", "api", "generic"
    openmemory_error_status: int = 500,
) -> Dict[str, Any]:
    """
    创建测试用的依赖集合

    Args:
        team_write_enabled: 是否启用团队写入
        policy_json: 策略 JSON
        dedup_hit: 是否模拟 dedup 命中
        openmemory_success: OpenMemory 是否成功
        openmemory_error_type: OpenMemory 错误类型
        openmemory_error_status: API 错误状态码

    Returns:
        包含 config, db, client, adapter 的字典
    """
    # 创建配置
    config = FakeGatewayConfig()

    # 创建 fake DB
    db = FakeLogbookDatabase()
    db.configure_settings(
        team_write_enabled=team_write_enabled,
        policy_json=policy_json or {},
    )

    # 创建 fake adapter
    adapter = FakeLogbookAdapter()
    if dedup_hit:
        adapter.configure_dedup_hit()
    else:
        adapter.configure_dedup_miss()

    # 创建 fake client
    client = FakeOpenMemoryClient()
    if openmemory_success:
        client.configure_store_success(memory_id="fake_memory_id")
        client.configure_search_success()
    elif openmemory_error_type == "connection":
        client.configure_store_connection_error("连接超时")
        client.configure_search_connection_error("连接超时")
    elif openmemory_error_type == "api":
        client.configure_store_api_error(status_code=openmemory_error_status)
        client.configure_search_api_error(status_code=openmemory_error_status)
    elif openmemory_error_type == "generic":
        client.configure_store_generic_error()

    return {
        "config": config,
        "db": db,
        "client": client,
        "adapter": adapter,
    }
