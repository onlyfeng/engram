"""
test_collection_id_support.py - 测试 collection_id 支持

测试场景：
1. collection_naming.to_pgvector_table_name() 生成正确的动态表名
2. pgvector_backend 接受 collection_id 并生成正确表名
3. pgvector_backend 动态表名校验（前缀模式）
4. step3_backend_factory 正确传递 collection_id
"""

import pytest
import re


class TestCollectionNaming:
    """测试 collection_naming 模块"""

    def test_to_pgvector_table_name_basic(self):
        """测试基本 collection_id 转换为 PGVector 表名"""
        from step3_seekdb_rag_hybrid.collection_naming import to_pgvector_table_name

        # 基本格式: project:version:model
        table_name = to_pgvector_table_name("proj1:v2:bge-m3")
        assert table_name == "step3_chunks_proj1_v2_bge_m3"

    def test_to_pgvector_table_name_with_version_tag(self):
        """测试带版本标签的 collection_id 转换"""
        from step3_seekdb_rag_hybrid.collection_naming import to_pgvector_table_name

        table_name = to_pgvector_table_name("default:v1:bge-m3:20260128T120000")
        assert table_name == "step3_chunks_default_v1_bge_m3_20260128t120000"

    def test_to_pgvector_table_name_special_chars(self):
        """测试特殊字符处理"""
        from step3_seekdb_rag_hybrid.collection_naming import to_pgvector_table_name

        # 冒号和连字符应被替换为下划线
        table_name = to_pgvector_table_name("my-project:v1:openai-ada-002")
        assert table_name == "step3_chunks_my_project_v1_openai_ada_002"

    def test_to_pgvector_table_name_uppercase(self):
        """测试大写字母处理（应转小写）"""
        from step3_seekdb_rag_hybrid.collection_naming import to_pgvector_table_name

        table_name = to_pgvector_table_name("Proj1:V2:BGE-M3")
        assert table_name == "step3_chunks_proj1_v2_bge_m3"

    def test_to_pgvector_table_name_length_limit(self):
        """测试超长 collection_id 被正确截断（PostgreSQL 最大 63 字符）"""
        from step3_seekdb_rag_hybrid.collection_naming import (
            to_pgvector_table_name,
            POSTGRES_MAX_IDENTIFIER_LENGTH,
        )

        # 创建一个超长的 collection_id
        long_project = "a" * 50
        long_model = "very_long_embedding_model_name_that_exceeds_limit"
        long_collection_id = f"{long_project}:v1:{long_model}"
        
        table_name = to_pgvector_table_name(long_collection_id)
        
        # 验证表名不超过 63 字符
        assert len(table_name) <= POSTGRES_MAX_IDENTIFIER_LENGTH
        assert len(table_name) <= 63
        
        # 验证表名仍然以正确前缀开头
        assert table_name.startswith("step3_chunks_")
        
        # 验证表名只包含合法字符
        assert re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name)

    def test_to_pgvector_table_name_long_unique(self):
        """测试两个超长但不同的 collection_id 生成不同的表名"""
        from step3_seekdb_rag_hybrid.collection_naming import to_pgvector_table_name

        # 两个只在末尾不同的超长 collection_id
        base = "a" * 50 + ":v1:model_"
        collection_a = base + "suffix_a"
        collection_b = base + "suffix_b"
        
        table_a = to_pgvector_table_name(collection_a)
        table_b = to_pgvector_table_name(collection_b)
        
        # 两个表名都不超过 63 字符
        assert len(table_a) <= 63
        assert len(table_b) <= 63
        
        # 两个表名应该不同（通过 hash 区分）
        assert table_a != table_b

    def test_to_pgvector_table_name_numeric_prefix(self):
        """测试数字开头的 collection_id 被正确处理"""
        from step3_seekdb_rag_hybrid.collection_naming import to_pgvector_table_name

        # 数字开头的 collection_id（项目名以数字开头）
        collection_id = "123proj:v1:model"
        table_name = to_pgvector_table_name(collection_id)
        
        # 表名应以字母或下划线开头（step3_chunks_ 前缀保证了这一点）
        assert table_name[0].isalpha() or table_name[0] == '_'
        assert table_name.startswith("step3_chunks_")

    def test_to_pgvector_table_name_empty_parts(self):
        """测试空部分的 collection_id 处理"""
        from step3_seekdb_rag_hybrid.collection_naming import to_pgvector_table_name

        # 空部分会生成连续下划线，应该被正确处理
        collection_id = "proj::model"
        table_name = to_pgvector_table_name(collection_id)
        
        # 验证表名合法
        assert len(table_name) <= 63
        assert re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name)


class TestPGVectorBackendTableValidation:
    """测试 pgvector_backend 表名校验"""

    def test_validate_static_table_name(self):
        """测试静态白名单表名校验"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import validate_table_name

        # 白名单中的表名应通过
        assert validate_table_name("chunks") == "chunks"
        assert validate_table_name("chunks_dev") == "chunks_dev"
        assert validate_table_name("embeddings") == "embeddings"

    def test_validate_dynamic_table_name(self):
        """测试动态表名（step3_chunks_ 前缀）校验"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import validate_table_name

        # 动态表名应通过
        assert validate_table_name("step3_chunks_proj1_v1_bge_m3") == "step3_chunks_proj1_v1_bge_m3"
        assert validate_table_name("step3_chunks_default_v2_openai_ada_002") == "step3_chunks_default_v2_openai_ada_002"
        
    def test_validate_dynamic_table_name_with_version_tag(self):
        """测试带版本标签的动态表名校验"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import validate_table_name

        table_name = validate_table_name("step3_chunks_proj1_v1_bge_m3_20260128t120000")
        assert table_name == "step3_chunks_proj1_v1_bge_m3_20260128t120000"

    def test_reject_invalid_table_name(self):
        """测试拒绝非法表名"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import (
            validate_table_name,
            SQLInjectionError,
        )

        # 不在白名单且不是动态前缀的表名应被拒绝
        with pytest.raises(SQLInjectionError):
            validate_table_name("malicious_table")

        # 包含非法字符的表名应被拒绝
        with pytest.raises(SQLInjectionError):
            validate_table_name("table;DROP TABLE")

    def test_dynamic_table_pattern_security(self):
        """测试动态表名正则安全性（通过 validate_table_name 间接测试）"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import (
            validate_table_name,
            SQLInjectionError,
        )
        import re

        # 合法动态表名应通过
        assert validate_table_name("step3_chunks_abc") == "step3_chunks_abc"
        assert validate_table_name("step3_chunks_proj1_v1_model") == "step3_chunks_proj1_v1_model"
        
        # 非法动态表名应被拒绝
        with pytest.raises(SQLInjectionError):
            validate_table_name("other_chunks_abc")  # 错误前缀
        
        # 包含非法字符的表名应被拒绝（正则校验在 validate_identifier 中）
        with pytest.raises(SQLInjectionError):
            validate_table_name("step3_chunks_abc;drop")


class TestPGVectorBackendCollectionId:
    """测试 PGVectorBackend collection_id 支持"""

    def test_backend_with_collection_id(self):
        """测试使用 collection_id 初始化后端"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import PGVectorBackend

        backend = PGVectorBackend(
            connection_string="postgresql://test:test@localhost:5432/test",
            schema="step3",
            table_name="chunks",  # 回退值
            collection_id="proj1:v1:bge-m3",
        )

        # 验证表名由 collection_id 生成
        assert backend.table_name == "step3_chunks_proj1_v1_bge_m3"
        assert backend.collection_id == "proj1:v1:bge-m3"
        assert backend.canonical_id == "proj1:v1:bge-m3"

    def test_backend_without_collection_id(self):
        """测试不使用 collection_id 时使用静态表名"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import PGVectorBackend

        backend = PGVectorBackend(
            connection_string="postgresql://test:test@localhost:5432/test",
            schema="step3",
            table_name="chunks",
        )

        # 验证使用静态表名
        assert backend.table_name == "chunks"
        assert backend.collection_id is None
        assert backend.canonical_id is None

    def test_backend_collection_id_precedence(self):
        """测试 collection_id 优先于 table_name"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import PGVectorBackend

        backend = PGVectorBackend(
            connection_string="postgresql://test:test@localhost:5432/test",
            schema="step3",
            table_name="chunks",  # 这个应被忽略
            collection_id="myproject:v2:model",
        )

        # collection_id 应优先
        assert backend.table_name == "step3_chunks_myproject_v2_model"
        assert "chunks" not in backend.table_name or "myproject" in backend.table_name


class TestBackendFactoryCollectionId:
    """测试 step3_backend_factory collection_id 支持"""

    def test_create_pgvector_backend_with_collection_id(self):
        """测试工厂函数传递 collection_id"""
        import os
        
        # 设置环境变量
        os.environ["STEP3_PGVECTOR_DSN"] = "postgresql://test:test@localhost:5432/test"
        
        try:
            from step3_seekdb_rag_hybrid.step3_backend_factory import create_pgvector_backend

            backend = create_pgvector_backend(
                collection_id="factory_test:v1:bge-m3",
            )

            assert backend.collection_id == "factory_test:v1:bge-m3"
            assert backend.table_name == "step3_chunks_factory_test_v1_bge_m3"
        finally:
            # 清理环境变量
            if "STEP3_PGVECTOR_DSN" in os.environ:
                del os.environ["STEP3_PGVECTOR_DSN"]

    def test_create_backend_from_env_with_collection_id(self):
        """测试 create_backend_from_env 传递 collection_id"""
        import os
        
        # 设置环境变量
        os.environ["STEP3_INDEX_BACKEND"] = "pgvector"
        os.environ["STEP3_PGVECTOR_DSN"] = "postgresql://test:test@localhost:5432/test"
        
        try:
            from step3_seekdb_rag_hybrid.step3_backend_factory import (
                create_backend_from_env,
                BackendType,
            )

            backend = create_backend_from_env(
                backend_type=BackendType.PGVECTOR,
                collection_id="env_test:v1:model",
            )

            assert backend.collection_id == "env_test:v1:model"
        finally:
            # 清理环境变量
            for key in ["STEP3_INDEX_BACKEND", "STEP3_PGVECTOR_DSN"]:
                if key in os.environ:
                    del os.environ[key]


class TestSeekDBBackendCollectionId:
    """测试 SeekDBBackend collection_id 支持（已有实现，验证兼容性）"""

    def test_seekdb_backend_collection_id(self):
        """测试 SeekDBBackend 使用 collection_id"""
        from step3_seekdb_rag_hybrid.index_backend.seekdb_backend import SeekDBBackend, SeekDBConfig

        config = SeekDBConfig(host="localhost", port=19530)
        backend = SeekDBBackend(
            config=config,
            namespace="test",  # 回退值
            chunking_version="v1",
            embedding_model_id="model",
            collection_id="seekdb_test:v2:bge-m3",
        )

        assert backend.canonical_id == "seekdb_test:v2:bge-m3"

    def test_seekdb_backend_without_collection_id(self):
        """测试 SeekDBBackend 不使用 collection_id 时的命名"""
        from step3_seekdb_rag_hybrid.index_backend.seekdb_backend import SeekDBBackend, SeekDBConfig

        config = SeekDBConfig(host="localhost", port=19530)
        backend = SeekDBBackend(
            config=config,
            namespace="myns",
            chunking_version="v1",
            embedding_model_id="model",
        )

        # 应生成默认的 canonical_id
        assert "myns" in backend.canonical_id
        assert "v1" in backend.canonical_id


class TestResolveCollectionId:
    """测试 resolve_collection_id 函数"""

    def test_explicit_collection_takes_precedence(self):
        """测试显式指定的 collection 优先"""
        from step3_seekdb_rag_hybrid.active_collection import resolve_collection_id

        # 显式指定应直接返回
        result = resolve_collection_id(
            conn=None,
            backend_name="pgvector",
            project_key="proj1",
            embedding_model_id="bge-m3",
            explicit_collection_id="custom:v1:model",
        )
        assert result == "custom:v1:model"

    def test_active_collection_used_when_no_explicit(self, monkeypatch):
        """测试未指定 explicit 时使用 active_collection"""
        from step3_seekdb_rag_hybrid import active_collection as ac_module
        
        # 模拟 get_active_collection 返回一个 active collection
        def mock_get_active(conn, backend_name, project_key=None):
            return "active_proj:v2:bge-m3"
        
        monkeypatch.setattr(ac_module, "get_active_collection", mock_get_active)
        
        # 创建一个模拟的 conn 对象（只需要非 None）
        class MockConn:
            pass
        
        result = ac_module.resolve_collection_id(
            conn=MockConn(),
            backend_name="pgvector",
            project_key="proj1",
            embedding_model_id="bge-m3",
            explicit_collection_id=None,
        )
        assert result == "active_proj:v2:bge-m3"

    def test_default_collection_when_no_active(self, monkeypatch):
        """测试没有 active_collection 时使用默认命名"""
        from step3_seekdb_rag_hybrid import active_collection as ac_module
        
        # 模拟 get_active_collection 返回 None
        def mock_get_active(conn, backend_name, project_key=None):
            return None
        
        monkeypatch.setattr(ac_module, "get_active_collection", mock_get_active)
        
        class MockConn:
            pass
        
        result = ac_module.resolve_collection_id(
            conn=MockConn(),
            backend_name="pgvector",
            project_key="myproj",
            embedding_model_id="bge-m3",
            explicit_collection_id=None,
            chunking_version="v1",
        )
        # 默认命名应包含 project_key, chunking_version, embedding_model_id
        assert "myproj" in result
        assert "v1" in result
        assert "bge-m3" in result


class TestMigrationConfigBackfill:
    """测试 pgvector_collection_migrate 回填逻辑"""

    def test_migration_config_make_collection_id_with_project(self):
        """测试 MigrationConfig.make_collection_id_for_project 有 project_key 时"""
        import sys
        from pathlib import Path
        
        # 添加 scripts 目录到路径
        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from pgvector_collection_migrate import MigrationConfig
        
        config = MigrationConfig(
            chunking_version="v2",
            embedding_model_id="bge-m3",
            default_collection_id="default:v2:bge-m3",
        )
        
        # 有 project_key 时应生成 {project_key}:{chunking_version}:{embedding_model_id}
        result = config.make_collection_id_for_project("myproject")
        assert result == "myproject:v2:bge-m3"

    def test_migration_config_make_collection_id_without_project(self):
        """测试 MigrationConfig.make_collection_id_for_project 无 project_key 时"""
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from pgvector_collection_migrate import MigrationConfig
        
        config = MigrationConfig(
            chunking_version="v2",
            embedding_model_id="bge-m3",
            default_collection_id="default:v2:bge-m3",
        )
        
        # 无 project_key 时应返回 default_collection_id
        result = config.make_collection_id_for_project(None)
        assert result == "default:v2:bge-m3"
        
        result = config.make_collection_id_for_project("")
        assert result == "default:v2:bge-m3"

    def test_migration_config_from_env(self, monkeypatch):
        """测试 MigrationConfig.from_env 读取环境变量"""
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from pgvector_collection_migrate import MigrationConfig
        
        # 设置环境变量
        monkeypatch.setenv("CHUNKING_VERSION", "v3")
        monkeypatch.setenv("STEP3_EMBEDDING_MODEL", "openai-ada-002")
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")
        
        config = MigrationConfig.from_env()
        
        assert config.chunking_version == "v3"
        assert config.embedding_model_id == "openai-ada-002"

    def test_migration_config_env_defaults(self, monkeypatch):
        """测试 MigrationConfig.from_env 使用默认值"""
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from pgvector_collection_migrate import MigrationConfig
        
        # 清除可能存在的环境变量
        monkeypatch.delenv("CHUNKING_VERSION", raising=False)
        monkeypatch.delenv("STEP3_EMBEDDING_MODEL", raising=False)
        
        config = MigrationConfig.from_env()
        
        # 应使用默认值
        assert config.chunking_version == "v1"
        assert config.embedding_model_id == "nomodel"

    def test_backfill_rule_format(self):
        """测试回填规则生成正确的 collection_id 格式"""
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from pgvector_collection_migrate import MigrationConfig, make_collection_id
        
        # 验证回填规则与 collection_naming.make_collection_id 一致
        config = MigrationConfig(
            chunking_version="v2",
            embedding_model_id="bge-m3",
        )
        
        # 使用 config 方法生成
        result = config.make_collection_id_for_project("proj1")
        
        # 使用 make_collection_id 生成（应该一致）
        expected = make_collection_id(
            project_key="proj1",
            chunking_version="v2",
            embedding_model_id="bge-m3",
        )
        
        assert result == expected
        assert result == "proj1:v2:bge-m3"

    def test_make_collection_id_imported_correctly(self):
        """测试 make_collection_id 正确导入"""
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from pgvector_collection_migrate import make_collection_id
        
        # 验证函数可用
        result = make_collection_id(
            project_key="test",
            chunking_version="v1",
            embedding_model_id="model",
        )
        assert result == "test:v1:model"
        
        # 验证默认值
        result = make_collection_id()
        assert result == "default:v1:nomodel"


class TestIncrementalSyncUsesActiveCollection:
    """测试 seek_indexer 增量同步使用 active_collection"""

    def test_incremental_sync_resolves_to_active_collection(self, monkeypatch):
        """测试增量同步模式在未指定 --collection 时使用 active_collection"""
        from step3_seekdb_rag_hybrid import active_collection as ac_module
        from step3_seekdb_rag_hybrid import seek_indexer
        
        # 记录 resolve_collection_id 被调用时的参数
        resolve_calls = []
        original_resolve = ac_module.resolve_collection_id
        
        def mock_resolve(
            conn=None,
            backend_name=None,
            project_key=None,
            embedding_model_id=None,
            explicit_collection_id=None,
            chunking_version=None,
        ):
            resolve_calls.append({
                "conn": conn,
                "backend_name": backend_name,
                "project_key": project_key,
                "embedding_model_id": embedding_model_id,
                "explicit_collection_id": explicit_collection_id,
                "chunking_version": chunking_version,
            })
            # 模拟返回 active collection
            return "active_test:v1:model"
        
        monkeypatch.setattr(ac_module, "resolve_collection_id", mock_resolve)
        monkeypatch.setattr(seek_indexer, "resolve_collection_id", mock_resolve)
        
        # 模拟其他依赖
        class MockBackend:
            backend_name = "pgvector"
            canonical_id = "active_test:v1:model"
            collection_id = "active_test:v1:model"
        
        class MockConn:
            def cursor(self, **kwargs):
                return MockCursor()
            def commit(self):
                pass
            def rollback(self):
                pass
        
        class MockCursor:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def execute(self, *args, **kwargs):
                pass
            def fetchall(self):
                return []
            def fetchone(self):
                return None
        
        # 调用 run_incremental_sync，不指定 collection
        result = seek_indexer.run_incremental_sync(
            conn=MockConn(),
            source="patch_blobs",
            project_key="testproj",
            batch_size=10,
            dry_run=True,
            backend=MockBackend(),
            collection=None,  # 不指定 collection
            rebuild_backend_for_collection=False,
        )
        
        # 验证 resolve_collection_id 被调用
        assert len(resolve_calls) == 1
        call = resolve_calls[0]
        
        # 验证参数正确传递
        assert call["explicit_collection_id"] is None  # 未显式指定
        assert call["backend_name"] == "pgvector"
        assert call["project_key"] == "testproj"
        
        # 验证结果使用了解析后的 collection
        assert result.collection == "active_test:v1:model"


class TestShowActiveCollection:
    """测试 show_active_collection 诊断功能"""

    def test_show_active_with_active_collection(self, monkeypatch):
        """测试有 active_collection 时的显示"""
        from step3_seekdb_rag_hybrid import seek_indexer
        from step3_seekdb_rag_hybrid import active_collection as ac_module
        
        # Mock get_active_collection 返回一个 active collection
        def mock_get_active(conn, backend_name, project_key=None):
            return "test_proj:v1:bge-m3"
        
        monkeypatch.setattr(ac_module, "get_active_collection", mock_get_active)
        monkeypatch.setattr(seek_indexer, "get_active_collection", mock_get_active)
        
        # Mock connection
        class MockConn:
            pass
        
        result = seek_indexer.show_active_collection(
            conn=MockConn(),
            backend_name="pgvector",
            project_key="test_proj",
        )
        
        assert result.found is True
        assert result.active_collection == "test_proj:v1:bge-m3"
        assert result.backend_name == "pgvector"
        assert result.project_key == "test_proj"

    def test_show_active_without_active_collection(self, monkeypatch):
        """测试没有 active_collection 时的显示"""
        from step3_seekdb_rag_hybrid import seek_indexer
        from step3_seekdb_rag_hybrid import active_collection as ac_module
        
        # Mock get_active_collection 返回 None
        def mock_get_active(conn, backend_name, project_key=None):
            return None
        
        monkeypatch.setattr(ac_module, "get_active_collection", mock_get_active)
        monkeypatch.setattr(seek_indexer, "get_active_collection", mock_get_active)
        
        class MockConn:
            pass
        
        result = seek_indexer.show_active_collection(
            conn=MockConn(),
            backend_name="pgvector",
            project_key=None,
        )
        
        assert result.found is False
        assert result.active_collection is None
        assert result.backend_name == "pgvector"

    def test_show_active_to_dict(self, monkeypatch):
        """测试 ActiveCollectionInfo.to_dict() 方法"""
        from step3_seekdb_rag_hybrid.seek_indexer import ActiveCollectionInfo
        
        info = ActiveCollectionInfo(
            backend_name="pgvector",
            project_key="myproj",
            active_collection="myproj:v1:bge-m3",
            found=True,
        )
        
        result = info.to_dict()
        
        assert result["backend_name"] == "pgvector"
        assert result["project_key"] == "myproj"
        assert result["active_collection"] == "myproj:v1:bge-m3"
        assert result["found"] is True


class TestValidateCollection:
    """测试 validate_collection 诊断功能"""

    def test_validate_collection_success(self, monkeypatch):
        """测试验证成功的 collection"""
        from step3_seekdb_rag_hybrid import seek_indexer
        
        # Mock backend 对象
        class MockBackend:
            backend_name = "pgvector"
            
            def preflight_check(self):
                return {"passed": True, "errors": []}
            
            def health_check(self):
                return {"healthy": True}
            
            def get_stats(self):
                return {"count": 1000, "storage_mb": 50}
        
        # Mock create_backend_from_env
        def mock_create_backend(**kwargs):
            return MockBackend()
        
        monkeypatch.setattr(seek_indexer, "create_backend_from_env", mock_create_backend)
        
        class MockConn:
            pass
        
        result = seek_indexer.validate_collection(
            conn=MockConn(),
            collection_id="test:v1:model",
            backend_name="pgvector",
        )
        
        assert result.valid is True
        assert result.available is True
        assert result.preflight_passed is True
        assert result.backend_healthy is True
        assert result.backend_stats is not None
        assert result.backend_stats["count"] == 1000

    def test_validate_collection_preflight_failure(self, monkeypatch):
        """测试 preflight 失败的情况"""
        from step3_seekdb_rag_hybrid import seek_indexer
        
        class MockBackend:
            backend_name = "pgvector"
            
            def preflight_check(self):
                return {"passed": False, "errors": ["Connection refused"]}
            
            def health_check(self):
                return {"healthy": False}
        
        def mock_create_backend(**kwargs):
            return MockBackend()
        
        monkeypatch.setattr(seek_indexer, "create_backend_from_env", mock_create_backend)
        
        class MockConn:
            pass
        
        result = seek_indexer.validate_collection(
            conn=MockConn(),
            collection_id="test:v1:model",
        )
        
        assert result.valid is False
        assert result.available is False
        assert result.preflight_passed is False
        assert "Connection refused" in result.preflight_errors

    def test_validate_collection_no_backend(self, monkeypatch):
        """测试无法创建后端的情况"""
        from step3_seekdb_rag_hybrid import seek_indexer
        
        def mock_create_backend(**kwargs):
            return None
        
        monkeypatch.setattr(seek_indexer, "create_backend_from_env", mock_create_backend)
        
        class MockConn:
            pass
        
        result = seek_indexer.validate_collection(
            conn=MockConn(),
            collection_id="test:v1:model",
        )
        
        assert result.valid is False
        assert result.available is False
        assert "无法创建后端实例" in result.preflight_errors

    def test_validate_collection_to_dict(self):
        """测试 CollectionValidationResult.to_dict() 方法"""
        from step3_seekdb_rag_hybrid.seek_indexer import CollectionValidationResult
        
        result = CollectionValidationResult(
            collection_id="test:v1:model",
            backend_name="pgvector",
            valid=True,
            available=True,
            preflight_passed=True,
            backend_healthy=True,
            backend_stats={"count": 500},
            recommendations=["Collection 可用"],
        )
        
        d = result.to_dict()
        
        assert d["collection_id"] == "test:v1:model"
        assert d["backend_name"] == "pgvector"
        assert d["valid"] is True
        assert d["available"] is True
        assert d["preflight"]["passed"] is True
        assert d["backend"]["healthy"] is True
        assert d["backend"]["stats"]["count"] == 500
        assert "Collection 可用" in d["recommendations"]

    def test_validate_collection_with_boolean_preflight(self, monkeypatch):
        """测试 preflight_check 返回布尔值的情况"""
        from step3_seekdb_rag_hybrid import seek_indexer
        
        class MockBackend:
            backend_name = "pgvector"
            
            def preflight_check(self):
                return True  # 返回布尔值
            
            def health_check(self):
                return True  # 返回布尔值
        
        def mock_create_backend(**kwargs):
            return MockBackend()
        
        monkeypatch.setattr(seek_indexer, "create_backend_from_env", mock_create_backend)
        
        class MockConn:
            pass
        
        result = seek_indexer.validate_collection(
            conn=MockConn(),
            collection_id="test:v1:model",
        )
        
        assert result.preflight_passed is True
        assert result.backend_healthy is True
        assert result.valid is True

    def test_validate_collection_without_methods(self, monkeypatch):
        """测试后端没有 preflight/health/stats 方法的情况"""
        from step3_seekdb_rag_hybrid import seek_indexer
        
        class MinimalBackend:
            backend_name = "minimal"
            # 没有 preflight_check, health_check, get_stats 方法
        
        def mock_create_backend(**kwargs):
            return MinimalBackend()
        
        monkeypatch.setattr(seek_indexer, "create_backend_from_env", mock_create_backend)
        
        class MockConn:
            pass
        
        result = seek_indexer.validate_collection(
            conn=MockConn(),
            collection_id="test:v1:model",
        )
        
        # 无方法时应该假定通过
        assert result.preflight_passed is True
        assert result.backend_healthy is True
        assert result.valid is True
        assert result.available is True


class TestDiagnosticModesReadOnly:
    """测试诊断模式确保只读，不写入数据"""

    def test_show_active_does_not_write(self, monkeypatch):
        """测试 show_active 模式不会写入数据"""
        from step3_seekdb_rag_hybrid import seek_indexer
        from step3_seekdb_rag_hybrid import active_collection as ac_module
        
        write_calls = []
        
        # Mock set_active_collection 来跟踪是否被调用
        original_set = ac_module.set_active_collection
        def mock_set(conn, backend_name, collection_id, project_key=None):
            write_calls.append({
                "backend_name": backend_name,
                "collection_id": collection_id,
            })
            return original_set(conn, backend_name, collection_id, project_key)
        
        monkeypatch.setattr(ac_module, "set_active_collection", mock_set)
        monkeypatch.setattr(seek_indexer, "set_active_collection", mock_set)
        
        def mock_get_active(conn, backend_name, project_key=None):
            return "existing:v1:model"
        
        monkeypatch.setattr(ac_module, "get_active_collection", mock_get_active)
        monkeypatch.setattr(seek_indexer, "get_active_collection", mock_get_active)
        
        class MockConn:
            pass
        
        # 执行 show_active
        seek_indexer.show_active_collection(
            conn=MockConn(),
            backend_name="pgvector",
        )
        
        # 验证没有写入操作
        assert len(write_calls) == 0, "show_active 不应该调用 set_active_collection"

    def test_validate_collection_does_not_upsert(self, monkeypatch):
        """测试 validate_collection 模式不会写入索引数据"""
        from step3_seekdb_rag_hybrid import seek_indexer
        
        upsert_calls = []
        
        class MockBackend:
            backend_name = "pgvector"
            
            def preflight_check(self):
                return True
            
            def health_check(self):
                return True
            
            def get_stats(self):
                return {"count": 0}
            
            def upsert(self, docs):
                upsert_calls.append(docs)
                return len(docs)
        
        def mock_create_backend(**kwargs):
            return MockBackend()
        
        monkeypatch.setattr(seek_indexer, "create_backend_from_env", mock_create_backend)
        
        class MockConn:
            pass
        
        # 执行 validate_collection
        seek_indexer.validate_collection(
            conn=MockConn(),
            collection_id="test:v1:model",
        )
        
        # 验证没有 upsert 操作
        assert len(upsert_calls) == 0, "validate_collection 不应该调用 upsert"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
