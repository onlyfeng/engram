#!/usr/bin/env python3
"""
tests/ci/test_workflow_contract_real_repo_integration.py

集成测试：使用真实仓库文件验证 workflow contract 相关脚本的核心 API

测试范围：
1. validate_workflows.py - WorkflowContractValidator.validate()
2. check_workflow_contract_docs_sync.py - WorkflowContractDocsSyncChecker.check()
3. check_workflow_contract_version_policy.py - WorkflowContractVersionChecker.check()

涉及文件：
- scripts/ci/workflow_contract.v2.json
- scripts/ci/workflow_contract.v2.schema.json
- docs/ci_nightly_workflow_refactor/contract.md
- .github/workflows/ci.yml
- .github/workflows/nightly.yml
- Makefile

运行方式：
    pytest tests/ci/test_workflow_contract_real_repo_integration.py -v

依赖说明：
- jsonschema 是可选依赖，如未安装则跳过 schema 相关测试
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ============================================================================
# Path Constants
# ============================================================================

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()

# 合约文件路径
CONTRACT_PATH = PROJECT_ROOT / "scripts" / "ci" / "workflow_contract.v2.json"
SCHEMA_PATH = PROJECT_ROOT / "scripts" / "ci" / "workflow_contract.v2.schema.json"
DOC_PATH = PROJECT_ROOT / "docs" / "ci_nightly_workflow_refactor" / "contract.md"

# Workflow 文件路径
CI_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
NIGHTLY_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "nightly.yml"

# Makefile 路径
MAKEFILE_PATH = PROJECT_ROOT / "Makefile"


# ============================================================================
# Helper Functions
# ============================================================================


def check_required_files_exist() -> list[str]:
    """检查所有必需文件是否存在，返回缺失文件列表"""
    required_files = [
        CONTRACT_PATH,
        SCHEMA_PATH,
        DOC_PATH,
        CI_WORKFLOW_PATH,
        NIGHTLY_WORKFLOW_PATH,
        MAKEFILE_PATH,
    ]
    missing = [str(f) for f in required_files if not f.exists()]
    return missing


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="module")
def required_files_check() -> None:
    """确保所有必需文件存在（模块级 fixture）"""
    missing = check_required_files_exist()
    if missing:
        pytest.skip(f"Missing required files: {missing}")


# ============================================================================
# Test: validate_workflows.py 集成测试
# ============================================================================


class TestValidateWorkflowsIntegration:
    """使用真实仓库文件测试 validate_workflows.py"""

    def test_workflow_contract_validator_loads_contract(self, required_files_check: None) -> None:
        """测试 WorkflowContractValidator 能成功加载真实合约文件"""
        from scripts.ci.validate_workflows import WorkflowContractValidator

        validator = WorkflowContractValidator(
            contract_path=CONTRACT_PATH,
            workspace_root=PROJECT_ROOT,
        )

        # 加载合约应该成功
        assert validator.load_contract() is True
        assert validator.contract is not None
        assert "version" in validator.contract

    def test_workflow_contract_validator_validate_returns_result(
        self, required_files_check: None
    ) -> None:
        """测试 WorkflowContractValidator.validate() 返回有效结果"""
        from scripts.ci.validate_workflows import (
            ValidationResult,
            WorkflowContractValidator,
        )

        validator = WorkflowContractValidator(
            contract_path=CONTRACT_PATH,
            workspace_root=PROJECT_ROOT,
        )
        result = validator.validate()

        # 验证返回类型
        assert isinstance(result, ValidationResult)
        # 验证结果包含必需字段
        assert hasattr(result, "success")
        assert hasattr(result, "errors")
        assert hasattr(result, "warnings")
        assert hasattr(result, "validated_workflows")

    def test_workflow_contract_validator_validate_success(self, required_files_check: None) -> None:
        """测试 WorkflowContractValidator.validate() 在真实仓库上应该成功

        此测试验证当前仓库的 workflow 文件符合合约定义。
        如果此测试失败，说明 workflow 文件与合约不一致，需要检查：
        1. workflow 文件是否缺少合约中定义的 job/step
        2. 合约是否需要更新以反映 workflow 变更
        """
        from scripts.ci.validate_workflows import WorkflowContractValidator

        validator = WorkflowContractValidator(
            contract_path=CONTRACT_PATH,
            workspace_root=PROJECT_ROOT,
        )
        result = validator.validate()

        # 过滤关键错误（排除 WARNING 级别的问题）
        critical_error_types = {
            "workflow_not_found",
            "missing_job",
            "missing_job_id",
            "missing_step",
            "frozen_step_name_changed",
            "frozen_job_name_changed",
            "schema_error",
            "contract_job_ids_names_length_mismatch",
            "contract_job_ids_duplicate",
            "contract_required_job_id_duplicate",
            "contract_required_job_not_in_job_ids",
        }
        critical_errors = [e for e in result.errors if e.error_type in critical_error_types]

        # 断言无关键错误
        assert len(critical_errors) == 0, (
            f"Workflow contract validation failed with {len(critical_errors)} critical errors:\n"
            + "\n".join(
                f"  - [{e.error_type}] {e.workflow}:{e.key} - {e.message}" for e in critical_errors
            )
        )

        # 验证至少有一个 workflow 被验证
        assert len(result.validated_workflows) >= 1, "No workflows were validated"

    def test_workflow_contract_validator_validates_ci_workflow(
        self, required_files_check: None
    ) -> None:
        """测试 ci.yml workflow 被正确验证"""
        from scripts.ci.validate_workflows import WorkflowContractValidator

        validator = WorkflowContractValidator(
            contract_path=CONTRACT_PATH,
            workspace_root=PROJECT_ROOT,
        )
        result = validator.validate()

        # 验证 ci workflow 在已验证列表中
        assert "ci" in result.validated_workflows, (
            f"ci workflow not validated. Validated workflows: {result.validated_workflows}"
        )

    def test_workflow_contract_validator_validates_nightly_workflow(
        self, required_files_check: None
    ) -> None:
        """测试 nightly.yml workflow 被正确验证"""
        from scripts.ci.validate_workflows import WorkflowContractValidator

        validator = WorkflowContractValidator(
            contract_path=CONTRACT_PATH,
            workspace_root=PROJECT_ROOT,
        )
        result = validator.validate()

        # 验证 nightly workflow 在已验证列表中
        assert "nightly" in result.validated_workflows, (
            f"nightly workflow not validated. Validated workflows: {result.validated_workflows}"
        )


# ============================================================================
# Test: check_workflow_contract_docs_sync.py 集成测试
# ============================================================================


class TestWorkflowContractDocsSyncIntegration:
    """使用真实仓库文件测试 check_workflow_contract_docs_sync.py"""

    def test_docs_sync_checker_loads_files(self, required_files_check: None) -> None:
        """测试 WorkflowContractDocsSyncChecker 能成功加载真实文件"""
        from scripts.ci.check_workflow_contract_docs_sync import (
            WorkflowContractDocsSyncChecker,
        )

        checker = WorkflowContractDocsSyncChecker(
            contract_path=CONTRACT_PATH,
            doc_path=DOC_PATH,
        )

        # 加载应该成功
        assert checker.load_contract() is True
        assert checker.load_doc() is True
        assert checker.contract is not None
        assert checker.doc_content != ""

    def test_docs_sync_checker_check_returns_result(self, required_files_check: None) -> None:
        """测试 WorkflowContractDocsSyncChecker.check() 返回有效结果"""
        from scripts.ci.check_workflow_contract_docs_sync import (
            SyncResult,
            WorkflowContractDocsSyncChecker,
        )

        checker = WorkflowContractDocsSyncChecker(
            contract_path=CONTRACT_PATH,
            doc_path=DOC_PATH,
        )
        result = checker.check()

        # 验证返回类型
        assert isinstance(result, SyncResult)
        # 验证结果包含必需字段
        assert hasattr(result, "success")
        assert hasattr(result, "errors")
        assert hasattr(result, "warnings")
        assert hasattr(result, "checked_job_ids")
        assert hasattr(result, "checked_job_names")
        assert hasattr(result, "checked_frozen_steps")

    def test_docs_sync_checker_api_works(self, required_files_check: None) -> None:
        """测试 WorkflowContractDocsSyncChecker.check() API 正常工作

        此测试验证 API 能够正确执行并返回有效结果。
        它不验证仓库状态是否完美（那是 CI 门禁的职责）。
        """
        from scripts.ci.check_workflow_contract_docs_sync import (
            WorkflowContractDocsSyncChecker,
        )

        checker = WorkflowContractDocsSyncChecker(
            contract_path=CONTRACT_PATH,
            doc_path=DOC_PATH,
        )
        result = checker.check()

        # 验证 API 能执行并返回有效结果
        assert result is not None
        assert hasattr(result, "success")
        assert isinstance(result.errors, list)
        assert isinstance(result.warnings, list)

        # 验证检查器确实检查了内容（无论成功与否）
        total_checked = (
            len(result.checked_job_ids)
            + len(result.checked_job_names)
            + len(result.checked_frozen_steps)
            + len(result.checked_frozen_job_names)
        )
        assert total_checked > 0, "Checker did not check any items"

    def test_docs_sync_checker_critical_sections_exist(self, required_files_check: None) -> None:
        """测试文档同步检查器的关键错误类型

        验证：
        1. 合约和文档文件能成功加载（无 file category 错误）
        2. frozen_step_text 章节存在
        3. frozen_job_names 章节存在

        注意：job_id/job_name 的同步问题由 CI 门禁检查，此测试只验证关键章节存在。
        """
        from scripts.ci.check_workflow_contract_docs_sync import (
            WorkflowContractDocsSyncChecker,
        )

        checker = WorkflowContractDocsSyncChecker(
            contract_path=CONTRACT_PATH,
            doc_path=DOC_PATH,
        )
        result = checker.check()

        # 验证无文件加载错误
        file_errors = [e for e in result.errors if e.category == "file"]
        assert len(file_errors) == 0, f"File loading errors: {[e.message for e in file_errors]}"

        # 验证无关键章节缺失错误
        critical_section_errors = [
            e
            for e in result.errors
            if e.error_type in ("frozen_step_section_missing", "frozen_job_names_section_missing")
        ]
        assert len(critical_section_errors) == 0, "Critical section missing errors:\n" + "\n".join(
            f"  - {e.message}" for e in critical_section_errors
        )

    def test_docs_sync_checker_checks_job_ids(self, required_files_check: None) -> None:
        """测试文档同步检查器验证了 job_ids"""
        from scripts.ci.check_workflow_contract_docs_sync import (
            WorkflowContractDocsSyncChecker,
        )

        checker = WorkflowContractDocsSyncChecker(
            contract_path=CONTRACT_PATH,
            doc_path=DOC_PATH,
        )
        result = checker.check()

        # 验证至少检查了一些 job_ids
        assert len(result.checked_job_ids) >= 1, "No job_ids were checked"

    def test_docs_sync_checker_checks_frozen_steps(self, required_files_check: None) -> None:
        """测试文档同步检查器验证了 frozen_steps"""
        from scripts.ci.check_workflow_contract_docs_sync import (
            WorkflowContractDocsSyncChecker,
        )

        checker = WorkflowContractDocsSyncChecker(
            contract_path=CONTRACT_PATH,
            doc_path=DOC_PATH,
        )
        result = checker.check()

        # 验证至少检查了一些 frozen_steps
        assert len(result.checked_frozen_steps) >= 1, "No frozen_steps were checked"


# ============================================================================
# Test: check_workflow_contract_version_policy.py 集成测试
# ============================================================================


class TestWorkflowContractVersionPolicyIntegration:
    """使用真实仓库文件测试 check_workflow_contract_version_policy.py"""

    def test_version_checker_loads_files(self, required_files_check: None) -> None:
        """测试 WorkflowContractVersionChecker 能成功加载真实文件"""
        from scripts.ci.check_workflow_contract_version_policy import (
            WorkflowContractVersionChecker,
        )

        checker = WorkflowContractVersionChecker(
            project_root=PROJECT_ROOT,
        )

        # 加载应该成功
        assert checker.load_contract() is True
        assert checker.load_doc() is True
        assert checker.contract is not None
        assert checker.doc_content != ""

    def test_version_checker_check_returns_result(self, required_files_check: None) -> None:
        """测试 WorkflowContractVersionChecker.check() 返回有效结果"""
        from scripts.ci.check_workflow_contract_version_policy import (
            VersionCheckResult,
            WorkflowContractVersionChecker,
        )

        checker = WorkflowContractVersionChecker(
            project_root=PROJECT_ROOT,
        )
        # 使用空变更文件列表进行测试（不触发版本检查）
        result = checker.check(changed_files=[])

        # 验证返回类型
        assert isinstance(result, VersionCheckResult)
        # 验证结果包含必需字段
        assert hasattr(result, "success")
        assert hasattr(result, "errors")
        assert hasattr(result, "warnings")
        assert hasattr(result, "changed_critical_files")
        assert hasattr(result, "contract_version")
        assert hasattr(result, "contract_last_updated")

    def test_version_checker_no_critical_files_passes(self, required_files_check: None) -> None:
        """测试无关键文件变更时版本检查通过"""
        from scripts.ci.check_workflow_contract_version_policy import (
            WorkflowContractVersionChecker,
        )

        checker = WorkflowContractVersionChecker(
            project_root=PROJECT_ROOT,
        )
        # 使用非关键文件列表
        result = checker.check(changed_files=["README.md", "src/some_file.py"])

        # 无关键文件变更时应该成功
        assert result.success is True
        assert len(result.changed_critical_files) == 0

    def test_version_checker_extracts_version(self, required_files_check: None) -> None:
        """测试版本检查器能正确提取合约版本"""
        from scripts.ci.check_workflow_contract_version_policy import (
            WorkflowContractVersionChecker,
        )

        checker = WorkflowContractVersionChecker(
            project_root=PROJECT_ROOT,
        )
        checker.load_contract()

        # 验证版本字段被提取
        assert checker.result.contract_version != ""
        # 版本应该符合 SemVer 格式
        import re

        assert re.match(r"^\d+\.\d+\.\d+$", checker.result.contract_version), (
            f"Version '{checker.result.contract_version}' does not match SemVer format"
        )

    def test_version_checker_extracts_last_updated(self, required_files_check: None) -> None:
        """测试版本检查器能正确提取 last_updated 日期"""
        from scripts.ci.check_workflow_contract_version_policy import (
            WorkflowContractVersionChecker,
        )

        checker = WorkflowContractVersionChecker(
            project_root=PROJECT_ROOT,
        )
        checker.load_contract()

        # 验证 last_updated 字段被提取
        assert checker.result.contract_last_updated != ""
        # last_updated 应该符合 YYYY-MM-DD 格式
        import re

        assert re.match(r"^\d{4}-\d{2}-\d{2}$", checker.result.contract_last_updated), (
            f"last_updated '{checker.result.contract_last_updated}' does not match YYYY-MM-DD format"
        )


# ============================================================================
# Test: JSON Schema 验证（可选依赖）
# ============================================================================


class TestJsonSchemaValidation:
    """测试 JSON Schema 验证功能

    注意：jsonschema 是可选依赖，如未安装则跳过这些测试
    """

    @pytest.fixture
    def jsonschema_available(self) -> None:
        """检查 jsonschema 是否可用"""
        pytest.importorskip("jsonschema")

    def test_schema_file_exists(self, required_files_check: None) -> None:
        """测试 schema 文件存在"""
        assert SCHEMA_PATH.exists(), f"Schema file not found: {SCHEMA_PATH}"

    def test_schema_is_valid_json(self, required_files_check: None) -> None:
        """测试 schema 文件是有效的 JSON"""
        import json

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)

        assert isinstance(schema, dict)
        assert "$schema" in schema or "type" in schema

    def test_contract_validates_against_schema(
        self, required_files_check: None, jsonschema_available: None
    ) -> None:
        """测试合约文件符合 JSON Schema

        此测试验证 workflow_contract.v2.json 符合 workflow_contract.v2.schema.json
        定义的结构。
        """
        import json

        import jsonschema

        with open(CONTRACT_PATH, "r", encoding="utf-8") as f:
            contract = json.load(f)

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)

        # 验证合约符合 schema
        try:
            jsonschema.validate(contract, schema)
        except jsonschema.ValidationError as e:
            pytest.fail(f"Contract does not validate against schema: {e.message}")

    def test_validator_uses_schema_when_available(
        self, required_files_check: None, jsonschema_available: None
    ) -> None:
        """测试 WorkflowContractValidator 在 jsonschema 可用时使用 schema 验证"""
        from scripts.ci.validate_workflows import (
            HAS_JSONSCHEMA,
            WorkflowContractValidator,
        )

        # 验证 jsonschema 被检测到
        assert HAS_JSONSCHEMA is True

        validator = WorkflowContractValidator(
            contract_path=CONTRACT_PATH,
            workspace_root=PROJECT_ROOT,
        )
        validator.load_contract()

        # schema 验证应该成功
        result = validator.validate_schema()
        assert result is True, "Schema validation failed"


# ============================================================================
# Test: 跨模块一致性检查
# ============================================================================


class TestCrossModuleConsistency:
    """测试多个模块之间的一致性"""

    def test_all_validators_use_same_contract(self, required_files_check: None) -> None:
        """测试所有验证器使用相同的合约文件并读取相同版本"""
        import json

        from scripts.ci.check_workflow_contract_docs_sync import (
            WorkflowContractDocsSyncChecker,
        )
        from scripts.ci.check_workflow_contract_version_policy import (
            WorkflowContractVersionChecker,
        )
        from scripts.ci.validate_workflows import WorkflowContractValidator

        # 直接读取合约版本
        with open(CONTRACT_PATH, "r", encoding="utf-8") as f:
            contract = json.load(f)
        expected_version = contract.get("version", "")

        # 验证 WorkflowContractValidator
        validator1 = WorkflowContractValidator(
            contract_path=CONTRACT_PATH,
            workspace_root=PROJECT_ROOT,
        )
        validator1.load_contract()
        assert validator1.contract.get("version") == expected_version

        # 验证 WorkflowContractDocsSyncChecker
        checker2 = WorkflowContractDocsSyncChecker(
            contract_path=CONTRACT_PATH,
            doc_path=DOC_PATH,
        )
        checker2.load_contract()
        assert checker2.contract.get("version") == expected_version

        # 验证 WorkflowContractVersionChecker
        checker3 = WorkflowContractVersionChecker(
            project_root=PROJECT_ROOT,
        )
        checker3.load_contract()
        assert checker3.contract.get("version") == expected_version

    def test_makefile_targets_exist(self, required_files_check: None) -> None:
        """测试合约中引用的 make targets 在 Makefile 中存在"""
        import json

        from scripts.ci.validate_workflows import parse_makefile_targets

        # 读取合约中的 make targets
        with open(CONTRACT_PATH, "r", encoding="utf-8") as f:
            contract = json.load(f)

        targets_required = contract.get("make", {}).get("targets_required", [])
        if not targets_required:
            pytest.skip("No make.targets_required in contract")

        # 解析 Makefile
        makefile_targets = parse_makefile_targets(MAKEFILE_PATH)
        assert len(makefile_targets) > 0, "Failed to parse Makefile targets"

        # 验证所有 required targets 存在
        missing_targets = [t for t in targets_required if t not in makefile_targets]
        assert len(missing_targets) == 0, (
            f"Missing make targets: {missing_targets}\n"
            f"Available targets: {sorted(makefile_targets)[:20]}..."
        )


# ============================================================================
# Test: 文件存在性检查
# ============================================================================


class TestFileExistence:
    """测试所有必需文件存在"""

    def test_contract_file_exists(self) -> None:
        """测试合约文件存在"""
        assert CONTRACT_PATH.exists(), f"Contract file not found: {CONTRACT_PATH}"

    def test_schema_file_exists(self) -> None:
        """测试 schema 文件存在"""
        assert SCHEMA_PATH.exists(), f"Schema file not found: {SCHEMA_PATH}"

    def test_doc_file_exists(self) -> None:
        """测试文档文件存在"""
        assert DOC_PATH.exists(), f"Documentation file not found: {DOC_PATH}"

    def test_ci_workflow_exists(self) -> None:
        """测试 ci.yml workflow 存在"""
        assert CI_WORKFLOW_PATH.exists(), f"CI workflow not found: {CI_WORKFLOW_PATH}"

    def test_nightly_workflow_exists(self) -> None:
        """测试 nightly.yml workflow 存在"""
        assert NIGHTLY_WORKFLOW_PATH.exists(), (
            f"Nightly workflow not found: {NIGHTLY_WORKFLOW_PATH}"
        )

    def test_makefile_exists(self) -> None:
        """测试 Makefile 存在"""
        assert MAKEFILE_PATH.exists(), f"Makefile not found: {MAKEFILE_PATH}"
