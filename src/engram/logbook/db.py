"""
engram_logbook.db - 数据库连接模块

提供数据库连接和 SQL 执行功能。

================================================================================
架构约束（路线A - 多库方案）:
--------------------------------------------------------------------------------
- **生产环境**: 每个项目使用独立数据库，schema 名固定
- **测试环境**: 可使用 SchemaContext 的 schema_prefix 进行隔离
- rewrite_sql_for_schema() 等 prefix 相关功能仅用于测试场景
================================================================================

Schema 管理:
- 通过 SchemaContext 管理 schema 名称
- 连接时自动设置 search_path
- SQL 使用无前缀表名，依赖 search_path 解析
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import psycopg

from .config import Config, get_config
from .errors import DatabaseError, DbConnectionError
from .schema_context import SchemaContext, get_schema_context


class Database:
    """数据库连接管理类"""

    DEFAULT_KV_NAMESPACE = "default"

    def __init__(self, dsn: Optional[str] = None, config: Optional[Config] = None):
        """
        初始化数据库连接管理器

        Args:
            dsn: 数据库连接字符串（可选，优先于配置）
            config: Config 实例，为 None 时使用全局配置
        """
        if isinstance(dsn, Config) and config is None:
            config = dsn
            dsn = None
        self._dsn_override = dsn
        self._config = config or (Config.from_env() if dsn else get_config())
        self._conn: Optional[psycopg.Connection] = None

    @property
    def dsn(self) -> str:
        """获取数据库 DSN（支持环境变量兜底）"""
        if self._dsn_override:
            return self._dsn_override
        return get_dsn(self._config)

    def connect(self, autocommit: bool = False) -> psycopg.Connection:
        """
        建立数据库连接

        Args:
            autocommit: 是否启用自动提交模式

        Returns:
            psycopg.Connection 对象

        Raises:
            ConnectionError: 连接失败时抛出
        """
        self._conn = get_connection(
            dsn=self._dsn_override, config=self._config, autocommit=autocommit
        )
        return self._conn

    def disconnect(self) -> None:
        """关闭数据库连接"""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def connection(self) -> Optional[psycopg.Connection]:
        """当前连接"""
        return self._conn

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    def create_item(
        self,
        item_type: str,
        title: str,
        project_key: Optional[str] = None,
        **kwargs,
    ) -> int:
        """创建条目"""
        return create_item(
            item_type=item_type,
            title=title,
            project_key=project_key,
            config=self._config,
            dsn=self._dsn_override,
            **kwargs,
        )

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        """获取条目"""
        return get_item_by_id(
            item_id=item_id,
            config=self._config,
            dsn=self._dsn_override,
        )

    def add_event(
        self,
        item_id: int,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> int:
        """为条目添加事件"""
        return add_event(
            item_id=item_id,
            event_type=event_type,
            payload_json=payload,
            config=self._config,
            dsn=self._dsn_override,
            **kwargs,
        )

    def set_kv(
        self,
        key: str,
        value_json: Any,
        namespace: Optional[str] = None,
    ) -> None:
        """设置 KV"""
        ns = namespace or self.DEFAULT_KV_NAMESPACE
        set_kv(
            namespace=ns,
            key=key,
            value_json=value_json,
            config=self._config,
            dsn=self._dsn_override,
        )

    def get_kv(self, key: str, namespace: Optional[str] = None) -> Optional[Any]:
        """获取 KV"""
        ns = namespace or self.DEFAULT_KV_NAMESPACE
        return get_kv(
            namespace=ns,
            key=key,
            config=self._config,
            dsn=self._dsn_override,
        )

    @staticmethod
    def _mask_dsn(dsn: str) -> str:
        """隐藏 DSN 中的密码"""
        import re

        return re.sub(r":([^:@]+)@", ":***@", dsn)


# 全局数据库实例
_global_database: Optional[Database] = None


def get_database(config: Optional[Config] = None, reload: bool = False) -> Database:
    """
    获取全局数据库实例

    Args:
        config: Config 实例
        reload: 是否强制重新创建

    Returns:
        Database 实例
    """
    global _global_database
    if _global_database is None or reload:
        _global_database = Database(config)
    return _global_database


def reset_database() -> None:
    """重置全局数据库实例"""
    global _global_database
    if _global_database is not None:
        _global_database.disconnect()
        _global_database = None


# ---------- 迁移相关功能 ----------


# SQL 中需要处理的默认 schema 名（按依赖顺序）
DEFAULT_SCHEMA_NAMES = ["identity", "logbook", "scm", "analysis", "governance"]

# 默认 search_path（连接时设置，按优先级排序，public 作为兜底）
# 顺序：logbook 优先（核心业务表）-> identity -> scm -> analysis -> governance -> public
DEFAULT_SEARCH_PATH = ["logbook", "identity", "scm", "analysis", "governance", "public"]


def rewrite_sql_for_schema(
    sql_content: str,
    schema_context: Optional[SchemaContext] = None,
) -> str:
    """
    根据 SchemaContext 重写 SQL 中的 schema 名称。

    ============================================================================
    [测试专用] 此函数仅用于测试环境的 schema 隔离。
    生产环境使用固定 schema 名，不需要重写。
    ============================================================================

    处理以下模式：
    1. CREATE SCHEMA IF NOT EXISTS <schema> -> CREATE SCHEMA IF NOT EXISTS <prefix>_<schema>
    2. <schema>. 前缀 -> <prefix>_<schema>.
    3. table_schema = '<schema>' 检查 -> table_schema = '<prefix>_<schema>'
    4. schema_name = '<schema>' 检查 -> schema_name = '<prefix>_<schema>'

    Args:
        sql_content: 原始 SQL 内容
        schema_context: SchemaContext 实例，为 None 或无 prefix 时不进行重写

    Returns:
        重写后的 SQL 内容
    """
    import re

    if schema_context is None or schema_context.schema_prefix is None:
        return sql_content

    result = sql_content
    schema_map = schema_context.all_schemas  # {"identity": "prefix_identity", ...}

    for old_name, new_name in schema_map.items():
        if old_name == new_name:
            continue

        # 1. CREATE SCHEMA IF NOT EXISTS <schema>
        # 支持大小写不敏感匹配
        pattern = re.compile(
            rf"(CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+){old_name}(\s*;|\s+)", re.IGNORECASE
        )
        result = pattern.sub(rf"\g<1>{new_name}\2", result)

        # 2. <schema>. 前缀（表名引用）
        # 匹配 schema.table 模式，注意不要匹配已经替换过的
        # 使用词边界确保精确匹配
        pattern = re.compile(rf"\b{old_name}\.")
        result = pattern.sub(f"{new_name}.", result)

        # 3. table_schema = '<schema>' 检查
        # 支持单引号和双引号
        for quote in ["'", '"']:
            pattern = re.compile(rf"(table_schema\s*=\s*){quote}{old_name}{quote}", re.IGNORECASE)
            result = pattern.sub(rf"\g<1>{quote}{new_name}{quote}", result)

        # 4. schema_name = '<schema>' 检查
        for quote in ["'", '"']:
            pattern = re.compile(rf"(schema_name\s*=\s*){quote}{old_name}{quote}", re.IGNORECASE)
            result = pattern.sub(rf"\g<1>{quote}{new_name}{quote}", result)

        # 5. AND table_name = 'xxx' 保持不变（表名不需要重写）

        # 6. 处理 regclass 转换，如 'scm.patch_blobs'::regclass
        for quote in ["'", '"']:
            pattern = re.compile(
                rf"{quote}{old_name}\.([^{quote}]+){quote}(::regclass)", re.IGNORECASE
            )
            result = pattern.sub(rf"{quote}{new_name}.\1{quote}\2", result)

    return result


def get_dsn(config: Optional[Config] = None) -> str:
    """
    从配置中获取数据库 DSN

    优先级（高到低）：
    1. config 中的 postgres.dsn 显式配置
    2. 环境变量 POSTGRES_DSN
    3. 环境变量 TEST_PG_DSN（仅用于测试场景）

    Args:
        config: Config 实例，为 None 时使用全局配置

    Returns:
        数据库连接字符串

    Raises:
        ConfigError: 当 DSN 不存在时抛出
    """
    import os

    from .errors import ConfigError

    if config is None:
        config = get_config()

    # 优先级 1: 显式配置
    dsn = config.get("postgres.dsn")
    if dsn:
        return dsn

    # 优先级 2: 环境变量 POSTGRES_DSN
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        return dsn

    # 优先级 3: 环境变量 TEST_PG_DSN（仅用于测试）
    dsn = os.environ.get("TEST_PG_DSN")
    if dsn:
        return dsn

    # 无可用 DSN，抛出错误
    raise ConfigError(
        "未找到数据库 DSN 配置",
        {"checked": ["postgres.dsn", "POSTGRES_DSN", "TEST_PG_DSN"]},
    )


def get_connection(
    dsn: Optional[str] = None,
    config: Optional[Config] = None,
    autocommit: bool = False,
    schema_context: Optional[SchemaContext] = None,
    search_path: Optional[Union[List[str], str]] = None,
    statement_timeout_ms: Optional[int] = None,
) -> psycopg.Connection:
    """
    获取数据库连接。

    连接后会设置 search_path，优先级:
    1. 显式传入的 search_path 参数
    2. schema_context 提供的 search_path
    3. config 中的 postgres.search_path
    4. 全局 SchemaContext 的 search_path（当有 schema_prefix 时）
    5. 默认 search_path（DEFAULT_SEARCH_PATH: logbook, identity, scm, analysis, governance, public）

    注意：当确定 search_path 后，会自动追加 public（如果不存在）作为兜底。

    连接后还会设置 statement_timeout（可选），优先级:
    1. 显式传入的 statement_timeout_ms 参数
    2. 环境变量 ENGRAM_PG_STATEMENT_TIMEOUT_MS

    Args:
        dsn: 数据库连接字符串，为 None 时从配置读取
        config: Config 实例，仅当 dsn 为 None 时使用
        autocommit: 是否启用自动提交模式
        schema_context: SchemaContext 实例，用于多租户隔离
        search_path: 显式指定的 search_path（列表或逗号分隔字符串）
        statement_timeout_ms: 可选的语句超时时间（毫秒），设置后单条 SQL 超过该时间将被取消

    Returns:
        psycopg.Connection 对象

    Raises:
        ConnectionError: 连接失败时抛出
    """
    if config is None:
        config = get_config()

    if dsn is None:
        dsn = get_dsn(config)

    try:
        conn = psycopg.connect(dsn, autocommit=autocommit)
    except Exception as e:
        raise DbConnectionError(
            f"数据库连接失败: {e}",
            {"error": str(e)},
        )

    # 确定 search_path，按优先级选择
    schemas: Optional[List[str]] = None

    # 优先级 1: 显式传入的 search_path
    if search_path is not None:
        if isinstance(search_path, list):
            schemas = search_path
        else:
            schemas = [s.strip() for s in str(search_path).split(",") if s.strip()]

    # 优先级 2: schema_context
    elif schema_context is not None:
        schemas = schema_context.search_path

    # 优先级 3: config 中的 postgres.search_path
    else:
        search_path_cfg = config.get("postgres.search_path")
        if search_path_cfg:
            if isinstance(search_path_cfg, list):
                schemas = search_path_cfg
            else:
                schemas = [s.strip() for s in str(search_path_cfg).split(",") if s.strip()]
        else:
            # 优先级 4: 尝试使用全局 SchemaContext
            try:
                global_ctx = get_schema_context()
                if global_ctx.schema_prefix is not None:
                    schemas = global_ctx.search_path
            except Exception:
                pass

            # 优先级 5: 使用默认 search_path（当所有来源都未提供时）
            if schemas is None:
                schemas = DEFAULT_SEARCH_PATH.copy()

    # 设置 search_path
    if schemas:
        # 确保 public 作为兜底
        if "public" not in schemas:
            schemas.append("public")

        search_path_value = ", ".join(schemas)

        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {search_path_value}")
        except Exception as e:
            conn.close()
            raise DbConnectionError(
                f"设置 search_path 失败: {e}",
                {"search_path": search_path_value, "error": str(e)},
            )

    # 设置 statement_timeout（可选）
    # 优先级：显式参数 > 环境变量 ENGRAM_PG_STATEMENT_TIMEOUT_MS
    import os

    timeout_ms = statement_timeout_ms
    if timeout_ms is None:
        env_timeout = os.environ.get("ENGRAM_PG_STATEMENT_TIMEOUT_MS")
        if env_timeout:
            try:
                timeout_ms = int(env_timeout)
            except ValueError:
                pass  # 忽略无效值

    if timeout_ms is not None and timeout_ms > 0:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout TO {timeout_ms}")
        except Exception as e:
            conn.close()
            raise DbConnectionError(
                f"设置 statement_timeout 失败: {e}",
                {"statement_timeout_ms": timeout_ms, "error": str(e)},
            )

    return conn


def execute_sql_file(
    conn: psycopg.Connection,
    sql_path: Path,
    schema_context: Optional[SchemaContext] = None,
    placeholders: Optional[dict] = None,
) -> None:
    """
    执行 SQL 文件，支持 schema 重写和占位符替换。

    注意：事务控制由 SQL 文件自身管理（BEGIN/COMMIT）。

    Args:
        conn: 数据库连接（需要 autocommit=True 以支持 SQL 文件中的事务控制）
        sql_path: SQL 文件路径
        schema_context: SchemaContext 实例，用于重写 SQL 中的 schema 名
        placeholders: 占位符替换字典，如 {"__OPENMEMORY_SCHEMA__": "myproj_openmemory"}
                     占位符将在执行前被替换为对应的值

    Raises:
        DatabaseError: SQL 执行失败时抛出
    """
    try:
        sql_content = sql_path.read_text(encoding="utf-8")
        # 过滤 psql 专用指令（\if/\endif 等），避免 psycopg 执行失败
        sql_lines = [
            line for line in sql_content.splitlines() if not line.lstrip().startswith("\\")
        ]
        sql_content = "\n".join(sql_lines)
        if ":target_schema" in sql_content:
            import os

            target_schema = os.environ.get("OM_PG_SCHEMA", "openmemory")
            sql_content = sql_content.replace(":target_schema", f"'{target_schema}'")

        # 如果提供了 schema_context，重写 SQL 中的 schema 名
        if schema_context is not None:
            sql_content = rewrite_sql_for_schema(sql_content, schema_context)

        # 如果提供了占位符字典，进行替换
        if placeholders:
            for placeholder, value in placeholders.items():
                sql_content = sql_content.replace(placeholder, value)

        with conn.cursor() as cur:
            cur.execute(sql_content)
    except psycopg.Error as e:
        raise DatabaseError(
            f"SQL 执行失败: {e}",
            {"sql_path": str(sql_path), "error": str(e)},
        )


# ============ Logbook 操作函数 ============

import json
from typing import Any, Dict


def create_item(
    item_type: str,
    title: str,
    scope_json: Optional[Dict] = None,
    status: str = "open",
    owner_user_id: Optional[str] = None,
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
    project_key: Optional[str] = None,
) -> int:
    """
    在 logbook.items 中创建新条目

    Args:
        item_type: 条目类型
        title: 标题
        scope_json: 范围元数据 (default: {})
        status: 状态 (default: 'open')
        owner_user_id: 所有者用户 ID
        config: 配置实例
        project_key: 项目标识（可选）

    Returns:
        创建的 item_id
    """
    scope = scope_json or {}

    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO items (item_type, title, project_key, scope_json, status, owner_user_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING item_id
                """,
                (item_type, title, project_key, json.dumps(scope), status, owner_user_id),
            )
            result = cur.fetchone()
            conn.commit()
            return result[0]
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"创建 item 失败: {e}",
            {"item_type": item_type, "title": title, "error": str(e)},
        )
    finally:
        conn.close()


def add_event(
    item_id: int,
    event_type: str,
    payload_json: Optional[Dict] = None,
    payload: Optional[Dict] = None,
    status_from: Optional[str] = None,
    status_to: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    source: str = "tool",
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
) -> int:
    """
    在 logbook.events 中添加事件

    如果提供了 status_to，同时更新 item 的状态

    Args:
        item_id: 条目 ID
        event_type: 事件类型
        payload_json: 事件负载 (default: {})
        payload: payload_json 的别名（兼容旧接口）
        status_from: 变更前状态
        status_to: 变更后状态（会更新 item 状态）
        actor_user_id: 操作者用户 ID
        source: 事件来源 (default: 'tool')
        config: 配置实例

    Returns:
        创建的 event_id
    """
    if payload_json is None and payload is not None:
        payload_json = payload
    payload = payload_json or {}

    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            # 插入事件
            cur.execute(
                """
                INSERT INTO events
                    (item_id, event_type, status_from, status_to, payload_json, actor_user_id, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING event_id
                """,
                (
                    item_id,
                    event_type,
                    status_from,
                    status_to,
                    json.dumps(payload),
                    actor_user_id,
                    source,
                ),
            )
            result = cur.fetchone()
            event_id = result[0]

            # 如果提供了 status_to，更新 item 状态
            if status_to is not None:
                cur.execute(
                    """
                    UPDATE items
                    SET status = %s, updated_at = now()
                    WHERE item_id = %s
                    """,
                    (status_to, item_id),
                )

            conn.commit()
            return event_id
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"添加事件失败: {e}",
            {"item_id": item_id, "event_type": event_type, "error": str(e)},
        )
    finally:
        conn.close()


def attach(
    item_id: int,
    kind: str,
    uri: str,
    sha256: str,
    size_bytes: Optional[int] = None,
    meta_json: Optional[Dict] = None,
    config: Optional[Config] = None,
) -> int:
    """
    在 logbook.attachments 中添加附件

    Args:
        item_id: 条目 ID
        kind: 附件类型 (patch/log/report/spec/etc)
        uri: 附件 URI
        sha256: SHA256 哈希值
        size_bytes: 文件大小（字节）
        meta_json: 元数据 (default: {})
        config: 配置实例

    Returns:
        创建的 attachment_id
    """
    meta = meta_json or {}

    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO attachments (item_id, kind, uri, sha256, size_bytes, meta_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING attachment_id
                """,
                (item_id, kind, uri, sha256, size_bytes, json.dumps(meta)),
            )
            result = cur.fetchone()
            conn.commit()
            return result[0]
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"添加附件失败: {e}",
            {"item_id": item_id, "kind": kind, "uri": uri, "error": str(e)},
        )
    finally:
        conn.close()


def set_kv(
    namespace: str,
    key: str,
    value_json: Any,
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
) -> bool:
    """
    在 logbook.kv 中设置键值对（upsert）

    Args:
        namespace: 命名空间
        key: 键名
        value_json: 值（将序列化为 JSON）
        config: 配置实例

    Returns:
        True 表示成功
    """
    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kv (namespace, key, value_json, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (namespace, key) DO UPDATE
                SET value_json = EXCLUDED.value_json, updated_at = now()
                """,
                (namespace, key, json.dumps(value_json)),
            )
            conn.commit()
            return True
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"设置 KV 失败: {e}",
            {"namespace": namespace, "key": key, "error": str(e)},
        )
    finally:
        conn.close()


def get_kv(
    namespace: str,
    key: str,
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
) -> Optional[Any]:
    """
    从 logbook.kv 获取键值对

    Args:
        namespace: 命名空间
        key: 键名
        config: 配置实例

    Returns:
        值，如果不存在返回 None
    """
    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT value_json FROM kv
                WHERE namespace = %s AND key = %s
                """,
                (namespace, key),
            )
            result = cur.fetchone()
            if result:
                return result[0]
            return None
    except psycopg.Error as e:
        raise DatabaseError(
            f"获取 KV 失败: {e}",
            {"namespace": namespace, "key": key, "error": str(e)},
        )
    finally:
        conn.close()


from typing import List


def get_items_with_latest_event(
    limit: int = 100,
    item_type: Optional[str] = None,
    status: Optional[str] = None,
    config: Optional[Config] = None,
) -> List[Dict[str, Any]]:
    """
    查询 logbook.items 并联表获取最近事件信息

    Args:
        limit: 返回条目数量上限
        item_type: 按 item_type 筛选
        status: 按状态筛选
        config: 配置实例

    Returns:
        包含 item 信息和最近事件信息的列表
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            # 使用子查询获取每个 item 的最近事件
            query = """
                SELECT
                    i.item_id,
                    i.item_type,
                    i.title,
                    i.scope_json,
                    i.status,
                    i.owner_user_id,
                    i.created_at,
                    i.updated_at,
                    le.event_id AS latest_event_id,
                    le.event_type AS latest_event_type,
                    le.created_at AS latest_event_ts
                FROM items i
                LEFT JOIN LATERAL (
                    SELECT event_id, event_type, created_at
                    FROM events e
                    WHERE e.item_id = i.item_id
                    ORDER BY e.created_at DESC
                    LIMIT 1
                ) le ON true
                WHERE 1=1
            """
            params: List[Any] = []

            if item_type:
                query += " AND i.item_type = %s"
                params.append(item_type)

            if status:
                query += " AND i.status = %s"
                params.append(status)

            query += " ORDER BY COALESCE(le.created_at, i.updated_at, i.created_at) DESC"
            query += " LIMIT %s"
            params.append(limit)

            cur.execute(query, params)
            rows = cur.fetchall()

            results = []
            for row in rows:
                results.append(
                    {
                        "item_id": row[0],
                        "item_type": row[1],
                        "title": row[2],
                        "scope_json": row[3],
                        "status": row[4],
                        "owner_user_id": row[5],
                        "created_at": row[6],
                        "updated_at": row[7],
                        "latest_event_id": row[8],
                        "latest_event_type": row[9],
                        "latest_event_ts": row[10],
                    }
                )

            return results

    except psycopg.Error as e:
        raise DatabaseError(
            f"查询 items 失败: {e}",
            {"limit": limit, "error": str(e)},
        )
    finally:
        conn.close()


def get_item_by_id(
    item_id: int,
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    根据 item_id 获取单个 item

    Args:
        item_id: 条目 ID
        config: 配置实例

    Returns:
        item 信息字典，不存在返回 None
    """
    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT item_id, item_type, title, project_key, scope_json, status, owner_user_id, created_at, updated_at
                FROM items
                WHERE item_id = %s
                """,
                (item_id,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "item_id": row[0],
                    "id": row[0],
                    "item_type": row[1],
                    "title": row[2],
                    "project_key": row[3],
                    "scope_json": row[4],
                    "status": row[5],
                    "owner_user_id": row[6],
                    "created_at": row[7],
                    "updated_at": row[8],
                }
            return None
    except psycopg.Error as e:
        raise DatabaseError(
            f"获取 item 失败: {e}",
            {"item_id": item_id, "error": str(e)},
        )
    finally:
        conn.close()


# ============ Analysis 操作函数 ============


def query_knowledge_candidates(
    keyword: str,
    top_k: int = 10,
    evidence_filter: Optional[str] = None,
    space_filter: Optional[str] = None,
    config: Optional[Config] = None,
) -> List[Dict[str, Any]]:
    """
    从 analysis.knowledge_candidates 表按关键词查询知识候选项

    使用 ILIKE 对 title 和 content_md 进行模糊匹配。

    Args:
        keyword: 搜索关键词（将使用 ILIKE 匹配 title 和 content_md）
        top_k: 返回结果数量上限（默认 10）
        evidence_filter: 可选，按 evidence_refs_json 过滤（使用 JSON 包含匹配）
        space_filter: 可选，按 target_space 过滤（需要关联 write_audit 表）
        config: 配置实例

    Returns:
        知识候选项列表，每项包含 candidate_id, kind, title, content_md,
        confidence, evidence_refs_json, created_at 等字段
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            # 构建基础查询
            query = """
                SELECT
                    kc.candidate_id,
                    kc.run_id,
                    kc.kind,
                    kc.title,
                    kc.content_md,
                    kc.confidence,
                    kc.evidence_refs_json,
                    kc.promote_suggested,
                    kc.created_at
                FROM analysis.knowledge_candidates kc
                WHERE (
                    kc.title ILIKE %s
                    OR kc.content_md ILIKE %s
                )
            """
            # 构建 ILIKE 模式
            like_pattern = f"%{keyword}%"
            params: List[Any] = [like_pattern, like_pattern]

            # 添加 evidence_filter（如果提供）
            if evidence_filter:
                # 使用 JSONB 包含操作符 @> 或文本匹配
                query += " AND kc.evidence_refs_json::text ILIKE %s"
                params.append(f"%{evidence_filter}%")

            # 添加 space_filter（如果提供，需要关联 write_audit）
            # 注意：knowledge_candidates 本身没有 space 字段，
            # 但可以通过 evidence_refs_json 中的引用或其他关联方式过滤
            # 这里简化为通过 evidence_refs_json 文本匹配
            if space_filter:
                query += " AND kc.evidence_refs_json::text ILIKE %s"
                params.append(f"%{space_filter}%")

            # 按创建时间降序排列，并限制返回数量
            query += " ORDER BY kc.created_at DESC LIMIT %s"
            params.append(top_k)

            cur.execute(query, params)
            rows = cur.fetchall()

            results = []
            for row in rows:
                results.append(
                    {
                        "candidate_id": row[0],
                        "run_id": row[1],
                        "kind": row[2],
                        "title": row[3],
                        "content_md": row[4],
                        "confidence": row[5],
                        "evidence_refs_json": row[6],
                        "promote_suggested": row[7],
                        "created_at": row[8],
                    }
                )

            return results

    except psycopg.Error as e:
        raise DatabaseError(
            f"查询 knowledge_candidates 失败: {e}",
            {"keyword": keyword, "top_k": top_k, "error": str(e)},
        )
    finally:
        conn.close()
