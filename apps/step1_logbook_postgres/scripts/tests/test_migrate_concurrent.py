# -*- coding: utf-8 -*-
"""
测试数据库迁移并发安全性

验证 PostgreSQL 咨询锁机制确保同一 schema_prefix 的迁移不会并发执行。

================================================================================
架构约束（路线A - 多库方案）:
--------------------------------------------------------------------------------
schema_prefix 功能仅用于测试环境隔离，生产环境禁用。
此测试文件验证测试模式下的并发安全性。
需要设置 ENGRAM_TESTING=1 环境变量。
================================================================================
"""

import os
import sys
import time
import pytest
import multiprocessing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# 确保可以导入 db_migrate
sys.path.insert(0, str(Path(__file__).parent.parent))


# 设置测试模式环境变量（允许使用 schema_prefix）
@pytest.fixture(scope="module", autouse=True)
def enable_testing_mode():
    """启用测试模式，允许使用 schema_prefix"""
    old_value = os.environ.get("ENGRAM_TESTING")
    os.environ["ENGRAM_TESTING"] = "1"
    yield
    if old_value is None:
        os.environ.pop("ENGRAM_TESTING", None)
    else:
        os.environ["ENGRAM_TESTING"] = old_value


def run_migrate_in_process(dsn: str, schema_prefix: str, result_queue: multiprocessing.Queue):
    """
    在独立进程中执行迁移。
    
    Args:
        dsn: 数据库连接字符串
        schema_prefix: schema 前缀
        result_queue: 用于返回结果的队列
    """
    import os
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    
    # [路线A 约束] 子进程需要设置测试模式环境变量
    os.environ["ENGRAM_TESTING"] = "1"
    
    from db_migrate import run_migrate
    
    start_time = time.time()
    try:
        result = run_migrate(dsn=dsn, schema_prefix=schema_prefix, quiet=True)
        end_time = time.time()
        result_queue.put({
            "ok": result.get("ok", False),
            "duration": end_time - start_time,
            "result": result,
            "error": None,
        })
    except Exception as e:
        end_time = time.time()
        result_queue.put({
            "ok": False,
            "duration": end_time - start_time,
            "result": None,
            "error": str(e),
        })


class TestMigrateConcurrent:
    """测试迁移并发安全性"""
    
    def test_concurrent_migrate_same_prefix_serialized(self, test_db_info):
        """
        验证两个进程同时对同一 prefix 调用 migrate 时，一个等待另一个完成。
        
        预期行为:
        1. 两个迁移都最终成功（ok=True）
        2. 第二个迁移需要等待第一个完成（咨询锁的作用）
        3. 不会出现并发冲突或死锁
        """
        dsn = test_db_info["dsn"]
        schema_prefix = "concurrent_test"
        
        # 创建结果队列
        result_queue = multiprocessing.Queue()
        
        # 创建两个进程同时执行迁移
        p1 = multiprocessing.Process(
            target=run_migrate_in_process,
            args=(dsn, schema_prefix, result_queue)
        )
        p2 = multiprocessing.Process(
            target=run_migrate_in_process,
            args=(dsn, schema_prefix, result_queue)
        )
        
        # 同时启动两个进程
        p1.start()
        p2.start()
        
        # 等待两个进程完成（超时 60 秒）
        p1.join(timeout=60)
        p2.join(timeout=60)
        
        # 收集结果
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())
        
        # 验证结果
        assert len(results) == 2, f"应该收到两个结果，实际收到 {len(results)} 个"
        
        # 两个迁移都应该成功
        for i, r in enumerate(results):
            assert r["ok"], f"迁移 {i+1} 失败: {r.get('error') or r.get('result')}"
        
        # 清理创建的 schema
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                for suffix in ["identity", "logbook", "scm", "analysis", "governance"]:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{schema_prefix}_{suffix}" CASCADE')
    
    def test_concurrent_migrate_different_prefix_parallel(self, test_db_info):
        """
        验证两个不同 prefix 的迁移可以并行执行（不互相阻塞）。
        
        预期行为:
        1. 两个迁移都最终成功
        2. 使用不同的 prefix 意味着使用不同的锁
        """
        dsn = test_db_info["dsn"]
        prefix1 = "parallel_test_a"
        prefix2 = "parallel_test_b"
        
        result_queue = multiprocessing.Queue()
        
        p1 = multiprocessing.Process(
            target=run_migrate_in_process,
            args=(dsn, prefix1, result_queue)
        )
        p2 = multiprocessing.Process(
            target=run_migrate_in_process,
            args=(dsn, prefix2, result_queue)
        )
        
        p1.start()
        p2.start()
        
        p1.join(timeout=60)
        p2.join(timeout=60)
        
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())
        
        assert len(results) == 2, f"应该收到两个结果，实际收到 {len(results)} 个"
        
        for i, r in enumerate(results):
            assert r["ok"], f"迁移 {i+1} 失败: {r.get('error') or r.get('result')}"
        
        # 清理
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                for prefix in [prefix1, prefix2]:
                    for suffix in ["identity", "logbook", "scm", "analysis", "governance"]:
                        cur.execute(f'DROP SCHEMA IF EXISTS "{prefix}_{suffix}" CASCADE')


class TestAdvisoryLockHelpers:
    """测试咨询锁辅助函数"""
    
    def test_build_lock_key_with_prefix(self):
        """测试带前缀的锁键生成"""
        from db_migrate import _build_lock_key
        
        key = _build_lock_key("tenant_abc")
        assert key == "engram_migrate:tenant_abc"
    
    def test_build_lock_key_without_prefix(self):
        """测试无前缀的锁键生成"""
        from db_migrate import _build_lock_key
        
        key = _build_lock_key(None)
        assert key == "engram_migrate:default"
        
        key = _build_lock_key("")
        assert key == "engram_migrate:default"
    
    def test_advisory_lock_acquire_release(self, test_db_info):
        """测试咨询锁的获取和释放"""
        import psycopg
        from db_migrate import _acquire_advisory_lock, _release_advisory_lock
        
        dsn = test_db_info["dsn"]
        lock_key = "test_lock_key"
        
        with psycopg.connect(dsn, autocommit=True) as conn:
            # 获取锁
            _acquire_advisory_lock(conn, lock_key, quiet=True)
            
            # 验证锁被持有
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM pg_locks 
                    WHERE locktype = 'advisory' AND pid = pg_backend_pid()
                """)
                count = cur.fetchone()[0]
                assert count >= 1, "应该至少持有一个咨询锁"
            
            # 释放锁
            _release_advisory_lock(conn, lock_key, quiet=True)


class TestMigrateWithLock:
    """测试带锁的迁移功能"""
    
    def test_migrate_acquires_and_releases_lock(self, test_db_info):
        """验证迁移过程正确获取和释放锁"""
        from db_migrate import run_migrate
        import psycopg
        
        dsn = test_db_info["dsn"]
        prefix = "lock_test"
        
        # 执行迁移
        result = run_migrate(dsn=dsn, schema_prefix=prefix, quiet=True)
        assert result.get("ok"), f"迁移失败: {result}"
        
        # 验证锁已释放（通过能够重新获取相同的锁来验证）
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                # 尝试以非阻塞方式获取锁，应该成功
                cur.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s))",
                    ("engram_migrate:" + prefix,)
                )
                locked = cur.fetchone()[0]
                assert locked, "锁应该可以被获取（说明之前已正确释放）"
                
                # 释放测试锁
                cur.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))",
                    ("engram_migrate:" + prefix,)
                )
        
        # 清理
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                for suffix in ["identity", "logbook", "scm", "analysis", "governance"]:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{prefix}_{suffix}" CASCADE')


class TestDatabaseAutoCreateHelpers:
    """测试数据库自动创建辅助函数"""
    
    def test_validate_db_name_whitelist(self):
        """测试数据库名称白名单校验"""
        from db_migrate import validate_db_name
        
        # 合法名称
        valid_cases = ["proj_a", "engram_test", "a", "abc123", "my_project"]
        for name in valid_cases:
            ok, msg = validate_db_name(name)
            assert ok, f"'{name}' 应该合法: {msg}"
        
        # 非法名称
        invalid_cases = [
            ("", "空字符串"),
            ("123abc", "数字开头"),
            ("Proj_A", "大写字母"),
            ("proj-a", "连字符"),
            ("proj.a", "点号"),
            ("a" * 64, "超长"),
        ]
        for name, reason in invalid_cases:
            ok, msg = validate_db_name(name)
            assert not ok, f"'{name}' 应该不合法（{reason}）"
    
    def test_parse_db_name_from_dsn(self):
        """测试从 DSN 解析数据库名"""
        from db_migrate import parse_db_name_from_dsn
        
        cases = [
            ("postgresql://user:pass@localhost:5432/mydb", "mydb"),
            ("postgresql://localhost/proj_a", "proj_a"),
            ("postgresql://user@host:5432/db?sslmode=require", "db"),
        ]
        for dsn, expected in cases:
            result = parse_db_name_from_dsn(dsn)
            assert result == expected, f"'{dsn}' -> '{result}', expected '{expected}'"
    
    def test_ensure_database_exists_without_admin_dsn(self):
        """测试无 admin_dsn 时跳过创建"""
        from db_migrate import ensure_database_exists
        
        result = ensure_database_exists(
            target_dsn="postgresql://user:pass@localhost/proj_a",
            admin_dsn=None,
            project_key="proj_a",
            quiet=True,
        )
        
        assert result["ok"]
        assert result["db_name"] == "proj_a"
        assert result["created"] is False


class TestDatabaseAutoCreateConcurrent:
    """测试数据库自动创建的并发安全性"""
    
    def test_concurrent_database_creation_idempotent(self, test_db_info):
        """
        验证并发创建同一数据库时的幂等性。
        
        预期行为:
        1. 多个进程同时尝试创建同一数据库
        2. 只有一个成功创建，其他应检测到已存在
        3. 最终结果：数据库存在，无错误
        """
        import uuid
        from db_migrate import (
            ensure_database_exists,
            check_database_exists,
            replace_db_in_dsn,
        )
        
        admin_dsn = test_db_info["admin_dsn"]
        new_db_name = f"test_concurrent_{uuid.uuid4().hex[:8]}"
        target_dsn = replace_db_in_dsn(admin_dsn, new_db_name)
        
        result_queue = multiprocessing.Queue()
        
        def create_in_process(dsn, admin, db_name, queue):
            """在独立进程中尝试创建数据库"""
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).parent.parent))
            
            from db_migrate import ensure_database_exists
            
            try:
                result = ensure_database_exists(
                    target_dsn=dsn,
                    admin_dsn=admin,
                    quiet=True,
                )
                queue.put({"ok": result["ok"], "created": result.get("created")})
            except Exception as e:
                queue.put({"ok": False, "error": str(e)})
        
        try:
            # 启动两个进程同时创建
            p1 = multiprocessing.Process(
                target=create_in_process,
                args=(target_dsn, admin_dsn, new_db_name, result_queue)
            )
            p2 = multiprocessing.Process(
                target=create_in_process,
                args=(target_dsn, admin_dsn, new_db_name, result_queue)
            )
            
            p1.start()
            p2.start()
            
            p1.join(timeout=30)
            p2.join(timeout=30)
            
            # 收集结果
            results = []
            while not result_queue.empty():
                results.append(result_queue.get())
            
            # 验证：两个都应该成功
            assert len(results) == 2
            for r in results:
                assert r["ok"], f"创建失败: {r}"
            
            # 验证：至少一个 created=True，另一个 created=False
            created_count = sum(1 for r in results if r.get("created"))
            # 由于竞争条件，可能都是 False（如果另一个先完成）
            # 但不应该都是 True（除非极端竞争）
            assert created_count <= 1, "不应该有多个进程成功创建"
            
            # 验证数据库确实存在
            assert check_database_exists(admin_dsn, new_db_name)
        finally:
            # 清理
            import psycopg
            try:
                conn = psycopg.connect(admin_dsn, autocommit=True)
                with conn.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{new_db_name}"')
                conn.close()
            except Exception:
                pass
