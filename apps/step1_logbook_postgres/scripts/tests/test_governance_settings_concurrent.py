# -*- coding: utf-8 -*-
"""
governance.get_or_create_settings 并发测试

验证:
- 并发下重复创建不报错（依赖 ON CONFLICT）
- 所有并发调用返回值一致
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List

import psycopg
import pytest

from engram_step1 import governance
from engram_step1.config import Config


class TestGetOrCreateSettingsConcurrent:
    """测试 get_or_create_settings 并发安全性"""

    def test_concurrent_create_no_error(self, migrated_db: dict):
        """
        并发调用 get_or_create_settings 不应报错
        
        多个线程同时对同一 project_key 调用 get_or_create_settings，
        所有调用应成功完成，不抛出异常。
        """
        project_key = f"test_concurrent_{uuid.uuid4().hex[:8]}"
        num_threads = 10
        errors: List[Exception] = []
        results: List[Dict[str, Any]] = []
        lock = threading.Lock()

        def create_settings():
            try:
                # 为每个线程创建独立的 Config 实例
                config = Config()
                config._data = {"postgres": {"dsn": migrated_db["dsn"]}}
                result = governance.get_or_create_settings(project_key, config=config)
                with lock:
                    results.append(result)
            except Exception as e:
                with lock:
                    errors.append(e)

        # 使用 ThreadPoolExecutor 并发执行
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(create_settings) for _ in range(num_threads)]
            for future in as_completed(futures):
                pass  # 等待所有任务完成

        # 断言：无错误
        assert len(errors) == 0, f"并发创建应无错误，实际错误: {errors}"
        
        # 断言：所有调用都返回结果
        assert len(results) == num_threads, f"应有 {num_threads} 个结果，实际: {len(results)}"

    def test_concurrent_create_consistent_results(self, migrated_db: dict):
        """
        并发调用 get_or_create_settings 返回值应一致
        
        所有并发调用应返回相同的设置（相同的 project_key, team_write_enabled, policy_json）。
        """
        project_key = f"test_consistent_{uuid.uuid4().hex[:8]}"
        num_threads = 10
        results: List[Dict[str, Any]] = []
        lock = threading.Lock()

        def create_settings():
            config = Config()
            config._data = {"postgres": {"dsn": migrated_db["dsn"]}}
            result = governance.get_or_create_settings(project_key, config=config)
            with lock:
                results.append(result)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(create_settings) for _ in range(num_threads)]
            for future in as_completed(futures):
                pass

        # 断言：所有结果的核心字段一致
        assert len(results) == num_threads
        
        first_result = results[0]
        for i, result in enumerate(results[1:], start=2):
            assert result["project_key"] == first_result["project_key"], \
                f"结果 {i} 的 project_key 不一致"
            assert result["team_write_enabled"] == first_result["team_write_enabled"], \
                f"结果 {i} 的 team_write_enabled 不一致"
            assert result["policy_json"] == first_result["policy_json"], \
                f"结果 {i} 的 policy_json 不一致"

    def test_concurrent_create_default_values(self, migrated_db: dict):
        """
        get_or_create_settings 应返回正确的默认值
        
        team_write_enabled=false, policy_json={}
        """
        project_key = f"test_defaults_{uuid.uuid4().hex[:8]}"
        
        config = Config()
        config._data = {"postgres": {"dsn": migrated_db["dsn"]}}
        
        result = governance.get_or_create_settings(project_key, config=config)
        
        assert result["project_key"] == project_key
        assert result["team_write_enabled"] is False
        assert result["policy_json"] == {}
        assert result["updated_by"] is None

    def test_get_or_create_idempotent(self, migrated_db: dict):
        """
        get_or_create_settings 应是幂等的
        
        多次调用应返回相同结果，不会修改已存在的设置。
        """
        project_key = f"test_idempotent_{uuid.uuid4().hex[:8]}"
        
        config = Config()
        config._data = {"postgres": {"dsn": migrated_db["dsn"]}}
        
        # 第一次调用
        result1 = governance.get_or_create_settings(project_key, config=config)
        
        # 第二次调用
        result2 = governance.get_or_create_settings(project_key, config=config)
        
        # 第三次调用
        result3 = governance.get_or_create_settings(project_key, config=config)
        
        # 断言：三次调用结果一致
        assert result1["project_key"] == result2["project_key"] == result3["project_key"]
        assert result1["team_write_enabled"] == result2["team_write_enabled"] == result3["team_write_enabled"]
        assert result1["policy_json"] == result2["policy_json"] == result3["policy_json"]

    def test_get_or_create_does_not_overwrite_existing(self, migrated_db: dict):
        """
        get_or_create_settings 不应覆盖已存在的设置
        
        先用 upsert_settings 设置自定义值，再调用 get_or_create_settings，
        应返回已存在的自定义值而非默认值。
        """
        project_key = f"test_no_overwrite_{uuid.uuid4().hex[:8]}"
        
        config = Config()
        config._data = {"postgres": {"dsn": migrated_db["dsn"]}}
        
        # 先使用 upsert_settings 设置自定义值
        governance.upsert_settings(
            project_key=project_key,
            team_write_enabled=True,
            policy_json={"custom": "value"},
            updated_by="test_user",
            config=config,
        )
        
        # 调用 get_or_create_settings
        result = governance.get_or_create_settings(project_key, config=config)
        
        # 断言：返回已存在的设置，而非默认值
        assert result["project_key"] == project_key
        assert result["team_write_enabled"] is True, "不应覆盖为默认的 false"
        assert result["policy_json"] == {"custom": "value"}, "不应覆盖为默认的 {}"
        assert result["updated_by"] == "test_user"

    def test_concurrent_with_existing_setting(self, migrated_db: dict):
        """
        并发调用 get_or_create_settings 时，若设置已存在，应返回已存在的值
        """
        project_key = f"test_concurrent_existing_{uuid.uuid4().hex[:8]}"
        
        config = Config()
        config._data = {"postgres": {"dsn": migrated_db["dsn"]}}
        
        # 预先创建设置
        governance.upsert_settings(
            project_key=project_key,
            team_write_enabled=True,
            policy_json={"preset": True},
            config=config,
        )
        
        num_threads = 10
        results: List[Dict[str, Any]] = []
        lock = threading.Lock()

        def create_settings():
            thread_config = Config()
            thread_config._data = {"postgres": {"dsn": migrated_db["dsn"]}}
            result = governance.get_or_create_settings(project_key, config=thread_config)
            with lock:
                results.append(result)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(create_settings) for _ in range(num_threads)]
            for future in as_completed(futures):
                pass

        # 断言：所有结果都是预设的值
        for result in results:
            assert result["team_write_enabled"] is True
            assert result["policy_json"] == {"preset": True}
