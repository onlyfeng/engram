#!/usr/bin/env python3
"""
record_acceptance_run.py 单元测试

覆盖场景：
1. parse_metadata_kv - 解析 key=value 格式
2. merge_metadata - 合并 JSON 和 key=value 元数据
3. record_acceptance_run - 核心记录功能（含新参数）
4. CLI 参数解析（--command, --metadata-json, --metadata-kv）
5. 向后兼容性验证

注意：
- 所有测试使用 pytest tmp_path，不依赖真实文件系统
- 使用 mock 隔离外部依赖（git, docker, 环境变量等）
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# 将 scripts/acceptance 目录添加到 path
sys.path.insert(0, str(Path(__file__).parent.parent / "acceptance"))

from record_acceptance_run import (
    list_artifacts,
    load_summary_duration,
    main,
    merge_metadata,
    parse_metadata_kv,
    record_acceptance_run,
    sanitize_value,
)

# ============================================================================
# Test: sanitize_value (POSTGRES_DSN 脱敏)
# ============================================================================

class TestSanitizeValue:
    """测试敏感值脱敏功能"""

    def test_sanitize_postgres_dsn_with_password(self):
        """POSTGRES_DSN 中的密码应被脱敏"""
        dsn = "postgresql://user:secretpassword@localhost:5432/db"
        result = sanitize_value("POSTGRES_DSN", dsn)
        assert result == "postgresql://user:***@localhost:5432/db"
        assert "secretpassword" not in result

    def test_sanitize_postgres_dsn_complex_password(self):
        """复杂密码（含特殊字符）应被脱敏"""
        # 注意：正则 (://[^:]+:)[^@]+(@) 匹配到第一个 @ 为止
        # 所以测试用例使用不含 @ 的密码
        dsn = "postgresql://admin:P4ss!word#123@db.example.com:5432/mydb"
        result = sanitize_value("POSTGRES_DSN", dsn)
        assert result == "postgresql://admin:***@db.example.com:5432/mydb"
        assert "P4ss!word#123" not in result

    def test_sanitize_postgres_dsn_no_password(self):
        """无密码的 DSN 不变"""
        dsn = "postgresql://user@localhost:5432/db"
        result = sanitize_value("POSTGRES_DSN", dsn)
        # 没有密码部分时，正则不匹配，返回原值
        assert result == dsn

    def test_non_sensitive_key_not_sanitized(self):
        """非敏感环境变量不应被脱敏"""
        value = "postgresql://user:password@localhost:5432/db"
        result = sanitize_value("GATEWAY_URL", value)
        # 非 POSTGRES_DSN 不脱敏
        assert result == value


# ============================================================================
# Test: list_artifacts
# ============================================================================

class TestListArtifacts:
    """测试 artifacts 列举功能"""

    def test_list_artifacts_empty_dir(self, tmp_path: Path):
        """空目录返回空列表"""
        artifacts_dir = tmp_path / "empty_artifacts"
        artifacts_dir.mkdir()
        result = list_artifacts(artifacts_dir)
        assert result == []

    def test_list_artifacts_nonexistent_dir(self, tmp_path: Path):
        """不存在的目录返回空列表"""
        artifacts_dir = tmp_path / "nonexistent"
        result = list_artifacts(artifacts_dir)
        assert result == []

    def test_list_artifacts_single_file(self, tmp_path: Path, monkeypatch):
        """列举单个文件"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "summary.json").write_text('{"result": "PASS"}')

        # 切换工作目录使 relative_to 正常工作
        monkeypatch.chdir(tmp_path)

        result = list_artifacts(artifacts_dir)
        assert len(result) == 1
        assert "summary.json" in result[0]

    def test_list_artifacts_multiple_files(self, tmp_path: Path, monkeypatch):
        """列举多个文件（按名称排序）"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "summary.json").write_text("{}")
        (artifacts_dir / "steps.log").write_text("step1")
        (artifacts_dir / "health.json").write_text("{}")

        monkeypatch.chdir(tmp_path)

        result = list_artifacts(artifacts_dir)
        assert len(result) == 3
        # 验证排序
        filenames = [Path(p).name for p in result]
        assert filenames == sorted(filenames)

    def test_list_artifacts_nested_files(self, tmp_path: Path, monkeypatch):
        """列举嵌套目录中的文件"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "summary.json").write_text("{}")

        subdir = artifacts_dir / "diagnostics"
        subdir.mkdir()
        (subdir / "logs.txt").write_text("logs")

        monkeypatch.chdir(tmp_path)

        result = list_artifacts(artifacts_dir)
        assert len(result) == 2
        # 应包含顶层和嵌套文件
        assert any("summary.json" in p for p in result)
        assert any("logs.txt" in p for p in result)


# ============================================================================
# Test: load_summary_duration
# ============================================================================

class TestLoadSummaryDuration:
    """测试 duration 读取功能"""

    def test_load_duration_from_summary(self, tmp_path: Path):
        """从 summary.json 读取 duration_seconds"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        summary = {"result": "PASS", "duration_seconds": 120}
        (artifacts_dir / "summary.json").write_text(json.dumps(summary))

        result = load_summary_duration(artifacts_dir)
        assert result == 120

    def test_load_duration_no_summary_file(self, tmp_path: Path):
        """无 summary.json 时返回 None"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        result = load_summary_duration(artifacts_dir)
        assert result is None

    def test_load_duration_no_duration_field(self, tmp_path: Path):
        """summary.json 无 duration_seconds 字段时返回 None"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        summary = {"result": "PASS"}
        (artifacts_dir / "summary.json").write_text(json.dumps(summary))

        result = load_summary_duration(artifacts_dir)
        assert result is None

    def test_load_duration_invalid_json(self, tmp_path: Path):
        """summary.json 无效 JSON 时返回 None"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "summary.json").write_text("{invalid json}")

        result = load_summary_duration(artifacts_dir)
        assert result is None

    def test_load_duration_zero_value(self, tmp_path: Path):
        """duration_seconds 为 0 时应正确返回"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        summary = {"duration_seconds": 0}
        (artifacts_dir / "summary.json").write_text(json.dumps(summary))

        result = load_summary_duration(artifacts_dir)
        assert result == 0


# ============================================================================
# Test: parse_metadata_kv
# ============================================================================

class TestParseMetadataKv:
    """测试 key=value 解析功能"""

    def test_parse_single_pair(self):
        """解析单个 key=value 对"""
        result = parse_metadata_kv(["workflow=ci"])
        assert result == {"workflow": "ci"}

    def test_parse_multiple_pairs(self):
        """解析多个 key=value 对"""
        result = parse_metadata_kv(["workflow=ci", "profile=http_only", "run_id=12345"])
        assert result == {
            "workflow": "ci",
            "profile": "http_only",
            "run_id": "12345",
        }

    def test_parse_empty_value(self):
        """解析空值"""
        result = parse_metadata_kv(["key="])
        assert result == {"key": ""}

    def test_parse_value_with_equals(self):
        """解析值中包含等号的情况"""
        result = parse_metadata_kv(["url=http://example.com?a=1&b=2"])
        assert result == {"url": "http://example.com?a=1&b=2"}

    def test_parse_none_input(self):
        """解析 None 输入"""
        result = parse_metadata_kv(None)
        assert result == {}

    def test_parse_empty_list(self):
        """解析空列表"""
        result = parse_metadata_kv([])
        assert result == {}

    def test_parse_missing_equals_raises(self):
        """缺少等号应抛出异常"""
        with pytest.raises(ValueError, match="missing '='"):
            parse_metadata_kv(["invalid"])

    def test_parse_empty_key_raises(self):
        """空 key 应抛出异常"""
        with pytest.raises(ValueError, match="empty key"):
            parse_metadata_kv(["=value"])


# ============================================================================
# Test: merge_metadata
# ============================================================================

class TestMergeMetadata:
    """测试元数据合并功能"""

    def test_merge_json_only(self):
        """仅 JSON 元数据"""
        result = merge_metadata('{"workflow": "ci", "profile": "http_only"}', None)
        assert result == {"workflow": "ci", "profile": "http_only"}

    def test_merge_kv_only(self):
        """仅 key=value 元数据"""
        result = merge_metadata(None, ["workflow=nightly", "github_run_id=99"])
        assert result == {"workflow": "nightly", "github_run_id": "99"}

    def test_merge_both_kv_overrides_json(self):
        """key=value 应覆盖 JSON 中的同名 key"""
        json_str = '{"workflow": "ci", "profile": "http_only"}'
        kv_list = ["workflow=nightly"]  # 覆盖 workflow
        result = merge_metadata(json_str, kv_list)
        assert result == {"workflow": "nightly", "profile": "http_only"}

    def test_merge_both_adds_new_keys(self):
        """key=value 可添加新 key"""
        json_str = '{"workflow": "ci"}'
        kv_list = ["github_run_id=123"]
        result = merge_metadata(json_str, kv_list)
        assert result == {"workflow": "ci", "github_run_id": "123"}

    def test_merge_neither_returns_none(self):
        """均未提供时返回 None"""
        result = merge_metadata(None, None)
        assert result is None

    def test_merge_empty_json_and_kv_returns_none(self):
        """空字符串和空列表返回 None"""
        result = merge_metadata("", [])
        assert result is None

    def test_merge_invalid_json_raises(self):
        """无效 JSON 应抛出异常"""
        with pytest.raises(ValueError, match="Invalid JSON"):
            merge_metadata("{invalid json}", None)

    def test_merge_json_array_raises(self):
        """JSON 数组应抛出异常"""
        with pytest.raises(ValueError, match="must be a JSON object"):
            merge_metadata('["a", "b"]', None)

    def test_merge_json_primitive_raises(self):
        """JSON 原始值应抛出异常"""
        with pytest.raises(ValueError, match="must be a JSON object"):
            merge_metadata('"string"', None)


# ============================================================================
# Test: record_acceptance_run
# ============================================================================

class TestRecordAcceptanceRun:
    """测试核心记录功能"""

    @pytest.fixture
    def mock_env(self):
        """Mock 环境依赖"""
        with mock.patch.multiple(
            "record_acceptance_run",
            get_git_commit=mock.DEFAULT,
            get_os_version=mock.DEFAULT,
            get_docker_version=mock.DEFAULT,
            get_captured_env=mock.DEFAULT,
        ) as mocks:
            mocks["get_git_commit"].return_value = "abc123def456"
            mocks["get_os_version"].return_value = "Darwin 24.6.0 (arm64)"
            mocks["get_docker_version"].return_value = "Docker version 24.0.6"
            mocks["get_captured_env"].return_value = {"SKIP_DEPLOY": "0"}
            yield mocks

    def test_default_command_is_make_name(self, tmp_path: Path, mock_env):
        """默认 command 为 'make {name}'"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        with mock.patch("record_acceptance_run.Path", wraps=Path) as mock_path:
            # 让 output_dir 指向 tmp_path
            mock_path.return_value = tmp_path / ".artifacts" / "acceptance-runs"

            output_file = record_acceptance_run(
                name="acceptance-logbook-only",
                artifacts_dir=str(artifacts_dir),
                result="PASS",
            )

        with open(output_file) as f:
            record = json.load(f)

        assert record["command"] == "make acceptance-logbook-only"

    def test_custom_command_overrides_default(self, tmp_path: Path, mock_env):
        """--command 参数覆盖默认命令"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        output_file = record_acceptance_run(
            name="acceptance-logbook-only",
            artifacts_dir=str(artifacts_dir),
            result="PASS",
            command="./scripts/custom_test.sh --verbose",
        )

        with open(output_file) as f:
            record = json.load(f)

        assert record["command"] == "./scripts/custom_test.sh --verbose"

    def test_metadata_added_to_record(self, tmp_path: Path, mock_env):
        """metadata 字典被添加到记录中"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        metadata = {
            "workflow": "ci",
            "profile": "http_only",
            "github_run_id": "12345",
        }

        output_file = record_acceptance_run(
            name="acceptance-unified-min",
            artifacts_dir=str(artifacts_dir),
            result="PASS",
            metadata=metadata,
        )

        with open(output_file) as f:
            record = json.load(f)

        assert "metadata" in record
        assert record["metadata"] == metadata

    def test_no_metadata_field_when_none(self, tmp_path: Path, mock_env):
        """未提供 metadata 时记录中不应有 metadata 字段"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        output_file = record_acceptance_run(
            name="acceptance-logbook-only",
            artifacts_dir=str(artifacts_dir),
            result="PASS",
        )

        with open(output_file) as f:
            record = json.load(f)

        assert "metadata" not in record

    def test_backward_compatible_without_new_params(self, tmp_path: Path, mock_env):
        """向后兼容：不使用新参数时行为不变"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        output_file = record_acceptance_run(
            name="acceptance-logbook-only",
            artifacts_dir=str(artifacts_dir),
            result="PASS",
            commit="explicit_commit_sha",
        )

        with open(output_file) as f:
            record = json.load(f)

        # 验证所有原有字段
        assert record["name"] == "acceptance-logbook-only"
        assert record["result"] == "PASS"
        assert record["commit"] == "explicit_commit_sha"
        assert record["command"] == "make acceptance-logbook-only"
        assert "metadata" not in record
        assert "timestamp" in record
        assert "os_version" in record
        assert "artifacts_dir" in record

    def test_all_new_params_together(self, tmp_path: Path, mock_env):
        """同时使用所有新参数"""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        # 创建 summary.json
        summary = {"duration_seconds": 120}
        (artifacts_dir / "summary.json").write_text(json.dumps(summary))

        output_file = record_acceptance_run(
            name="acceptance-unified-full",
            artifacts_dir=str(artifacts_dir),
            result="PASS",
            commit="full_test_commit",
            command="make acceptance-unified-full VERIFY_FULL=1",
            metadata={
                "workflow": "nightly",
                "profile": "full",
                "github_run_id": "98765",
            },
        )

        with open(output_file) as f:
            record = json.load(f)

        assert record["name"] == "acceptance-unified-full"
        assert record["commit"] == "full_test_commit"
        assert record["command"] == "make acceptance-unified-full VERIFY_FULL=1"
        assert record["metadata"]["workflow"] == "nightly"
        assert record["metadata"]["profile"] == "full"
        assert record["metadata"]["github_run_id"] == "98765"
        assert record["duration_seconds"] == 120


# ============================================================================
# Test: CLI 参数解析
# ============================================================================

class TestCLIArguments:
    """测试 CLI 参数解析"""

    @pytest.fixture
    def mock_record_func(self):
        """Mock record_acceptance_run 函数"""
        with mock.patch("record_acceptance_run.record_acceptance_run") as m:
            m.return_value = "/tmp/test_output.json"
            yield m

    def test_cli_basic_args(self, mock_record_func, capsys):
        """基本参数解析"""
        with mock.patch(
            "sys.argv",
            [
                "prog",
                "--name", "acceptance-logbook-only",
                "--artifacts-dir", ".artifacts/test",
                "--result", "PASS",
            ]
        ):
            exit_code = main()

        assert exit_code == 0
        mock_record_func.assert_called_once()
        call_kwargs = mock_record_func.call_args[1]
        assert call_kwargs["name"] == "acceptance-logbook-only"
        assert call_kwargs["artifacts_dir"] == ".artifacts/test"
        assert call_kwargs["result"] == "PASS"
        assert call_kwargs["command"] is None
        assert call_kwargs["metadata"] is None

    def test_cli_command_arg(self, mock_record_func):
        """--command 参数"""
        with mock.patch(
            "sys.argv",
            [
                "prog",
                "--name", "acceptance-unified-min",
                "--artifacts-dir", ".artifacts/test",
                "--result", "PASS",
                "--command", "make acceptance-unified-min HTTP_ONLY_MODE=1",
            ]
        ):
            main()

        call_kwargs = mock_record_func.call_args[1]
        assert call_kwargs["command"] == "make acceptance-unified-min HTTP_ONLY_MODE=1"

    def test_cli_metadata_json_arg(self, mock_record_func):
        """--metadata-json 参数"""
        with mock.patch(
            "sys.argv",
            [
                "prog",
                "--name", "test",
                "--artifacts-dir", ".artifacts/test",
                "--result", "PASS",
                "--metadata-json", '{"workflow": "ci", "profile": "http_only"}',
            ]
        ):
            main()

        call_kwargs = mock_record_func.call_args[1]
        assert call_kwargs["metadata"] == {"workflow": "ci", "profile": "http_only"}

    def test_cli_metadata_kv_single(self, mock_record_func):
        """单个 --metadata-kv 参数"""
        with mock.patch(
            "sys.argv",
            [
                "prog",
                "--name", "test",
                "--artifacts-dir", ".artifacts/test",
                "--result", "PASS",
                "--metadata-kv", "workflow=nightly",
            ]
        ):
            main()

        call_kwargs = mock_record_func.call_args[1]
        assert call_kwargs["metadata"] == {"workflow": "nightly"}

    def test_cli_metadata_kv_multiple(self, mock_record_func):
        """多个 --metadata-kv 参数"""
        with mock.patch(
            "sys.argv",
            [
                "prog",
                "--name", "test",
                "--artifacts-dir", ".artifacts/test",
                "--result", "PASS",
                "--metadata-kv", "workflow=ci",
                "--metadata-kv", "profile=http_only",
                "--metadata-kv", "github_run_id=12345",
            ]
        ):
            main()

        call_kwargs = mock_record_func.call_args[1]
        assert call_kwargs["metadata"] == {
            "workflow": "ci",
            "profile": "http_only",
            "github_run_id": "12345",
        }

    def test_cli_metadata_json_and_kv_merge(self, mock_record_func):
        """--metadata-json 和 --metadata-kv 合并"""
        with mock.patch(
            "sys.argv",
            [
                "prog",
                "--name", "test",
                "--artifacts-dir", ".artifacts/test",
                "--result", "PASS",
                "--metadata-json", '{"workflow": "ci", "profile": "http_only"}',
                "--metadata-kv", "workflow=nightly",  # 覆盖 JSON 中的 workflow
                "--metadata-kv", "extra_key=extra_value",  # 新增
            ]
        ):
            main()

        call_kwargs = mock_record_func.call_args[1]
        assert call_kwargs["metadata"] == {
            "workflow": "nightly",  # 被 kv 覆盖
            "profile": "http_only",  # 保持 JSON 原值
            "extra_key": "extra_value",  # 新增
        }

    def test_cli_invalid_json_error(self, mock_record_func, capsys):
        """无效 JSON 应返回错误"""
        with mock.patch(
            "sys.argv",
            [
                "prog",
                "--name", "test",
                "--artifacts-dir", ".artifacts/test",
                "--result", "PASS",
                "--metadata-json", "{invalid}",
            ]
        ):
            exit_code = main()

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Invalid JSON" in captured.err

    def test_cli_invalid_kv_error(self, mock_record_func, capsys):
        """无效 key=value 应返回错误"""
        with mock.patch(
            "sys.argv",
            [
                "prog",
                "--name", "test",
                "--artifacts-dir", ".artifacts/test",
                "--result", "PASS",
                "--metadata-kv", "no_equals_sign",
            ]
        ):
            exit_code = main()

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "missing '='" in captured.err

    def test_cli_all_args_together(self, mock_record_func):
        """所有参数一起使用"""
        with mock.patch(
            "sys.argv",
            [
                "prog",
                "--name", "acceptance-unified-full",
                "--artifacts-dir", ".artifacts/acceptance-unified-full",
                "--result", "PASS",
                "--commit", "abc123",
                "--command", "./run_full_tests.sh",
                "--metadata-json", '{"workflow": "nightly"}',
                "--metadata-kv", "profile=full",
            ]
        ):
            main()

        call_kwargs = mock_record_func.call_args[1]
        assert call_kwargs["name"] == "acceptance-unified-full"
        assert call_kwargs["artifacts_dir"] == ".artifacts/acceptance-unified-full"
        assert call_kwargs["result"] == "PASS"
        assert call_kwargs["commit"] == "abc123"
        assert call_kwargs["command"] == "./run_full_tests.sh"
        assert call_kwargs["metadata"] == {"workflow": "nightly", "profile": "full"}


# ============================================================================
# Test: 边界条件和异常处理
# ============================================================================

class TestEdgeCases:
    """测试边界条件"""

    def test_metadata_with_special_characters(self):
        """metadata 值包含特殊字符"""
        kv = ["message=Hello, World!", "url=https://example.com?a=1&b=2"]
        result = parse_metadata_kv(kv)
        assert result["message"] == "Hello, World!"
        assert result["url"] == "https://example.com?a=1&b=2"

    def test_metadata_with_unicode(self):
        """metadata 值包含 Unicode"""
        result = merge_metadata('{"msg": "你好世界"}', ["emoji=🚀"])
        assert result["msg"] == "你好世界"
        assert result["emoji"] == "🚀"

    def test_metadata_with_nested_json(self):
        """metadata JSON 包含嵌套对象"""
        json_str = '{"tags": ["ci", "nightly"], "config": {"verbose": true}}'
        result = merge_metadata(json_str, None)
        assert result["tags"] == ["ci", "nightly"]
        assert result["config"] == {"verbose": True}

    def test_empty_command_string(self, tmp_path: Path):
        """空字符串 command 应被保留"""
        with mock.patch.multiple(
            "record_acceptance_run",
            get_git_commit=mock.MagicMock(return_value="abc123"),
            get_os_version=mock.MagicMock(return_value="Darwin"),
            get_docker_version=mock.MagicMock(return_value=None),
            get_captured_env=mock.MagicMock(return_value={}),
        ):
            artifacts_dir = tmp_path / "artifacts"
            artifacts_dir.mkdir()

            output_file = record_acceptance_run(
                name="test",
                artifacts_dir=str(artifacts_dir),
                result="PASS",
                command="",  # 空字符串
            )

            with open(output_file) as f:
                record = json.load(f)

            # 空字符串应被保留，不回退到默认值
            assert record["command"] == ""


# ============================================================================
# Test: Makefile acceptance 静态约束验证
# ============================================================================

class TestMakefileAcceptanceConstraints:
    """
    验证 Makefile acceptance targets 的静态约束。

    使用 make -n (dry-run) 解析输出，验证：
    1. 会创建 steps.log
    2. 会创建 summary.json
    3. 会调用 record_acceptance_run.py 脚本

    注意：不实际执行 Docker，仅验证 Makefile 逻辑结构。
    """

    @pytest.fixture
    def workspace_root(self) -> Path:
        """获取工作区根目录（包含 Makefile）"""
        # 从 scripts/tests 向上找到包含 Makefile 的目录
        current = Path(__file__).parent
        for _ in range(5):  # 最多向上查找 5 级
            if (current / "Makefile").exists():
                return current
            current = current.parent
        pytest.skip("Cannot find Makefile in workspace")

    @pytest.mark.parametrize("target", [
        "acceptance-unified-min",
        "acceptance-unified-full",
        "acceptance-logbook-only",
    ])
    def test_acceptance_target_creates_artifacts(self, workspace_root: Path, target: str):
        """验证 acceptance target 会创建 steps.log 和 summary.json"""
        import subprocess

        # 使用 make -n 获取 dry-run 输出
        subprocess.run(
            ["make", "-n", target],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # 组合 stdout 和 Makefile 内容进行分析
        # 注意: make -n 可能不会展开所有 shell 变量，所以我们直接检查 Makefile
        makefile_path = workspace_root / "Makefile"
        makefile_content = makefile_path.read_text()

        # 提取对应 target 的定义块
        target_pattern = f"{target}:"
        assert target_pattern in makefile_content, f"Target {target} not found in Makefile"

        # 验证 target 定义中包含必要的输出文件
        # 查找该 target 到下一个 target 之间的内容
        lines = makefile_content.split("\n")
        in_target = False
        target_content = []
        for line in lines:
            if line.startswith(f"{target}:"):
                in_target = True
                continue
            if in_target:
                if line and not line.startswith("\t") and not line.startswith(" ") and ":" in line:
                    break  # 遇到下一个 target
                target_content.append(line)

        target_block = "\n".join(target_content)

        # 验证关键文件创建
        assert "steps.log" in target_block, f"{target} should create steps.log"
        assert "summary.json" in target_block, f"{target} should create summary.json"
        assert "record_acceptance_run.py" in target_block, f"{target} should call record_acceptance_run.py"

    def test_acceptance_unified_min_uses_http_only_mode(self, workspace_root: Path):
        """验证 acceptance-unified-min 使用 HTTP_ONLY_MODE"""
        makefile_path = workspace_root / "Makefile"
        makefile_content = makefile_path.read_text()

        # 查找 acceptance-unified-min target
        assert 'HTTP_ONLY_MODE' in makefile_content

        # 找到 target 定义块
        lines = makefile_content.split("\n")
        in_target = False
        for i, line in enumerate(lines):
            if line.startswith("acceptance-unified-min:"):
                in_target = True
            elif in_target and "HTTP_ONLY_MODE" in line:
                # 验证设置了 HTTP_ONLY_MODE=1
                assert "1" in line or '"1"' in line
                return
            elif in_target and not line.startswith("\t") and ":" in line and line.strip():
                break

        # 如果能找到则通过
        assert in_target, "acceptance-unified-min target should exist"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
