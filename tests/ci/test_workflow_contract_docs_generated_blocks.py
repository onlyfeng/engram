#!/usr/bin/env python3
"""
tests/ci/test_workflow_contract_docs_generated_blocks.py

测试受控块渲染结果与文档中实际内容的一致性。

测试策略：
1. 运行渲染器生成期望的块内容
2. 从实际文档中提取块内容
3. 断言期望与实际内容一致

受控块覆盖：
- contract.md: CI_JOB_TABLE, NIGHTLY_JOB_TABLE, FROZEN_JOB_NAMES_TABLE,
               FROZEN_STEP_NAMES_TABLE, MAKE_TARGETS_TABLE, LABELS_TABLE
- coupling_map.md: CI_JOBS_LIST, NIGHTLY_JOBS_LIST, MAKE_TARGETS_LIST
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.render_workflow_contract_docs import (
    CONTRACT_BLOCK_NAMES,
    COUPLING_MAP_BLOCK_NAMES,
    COUPLING_MAP_TARGET_REASON_TEXT,
    FROZEN_JOB_REASON_TEXT,
    FROZEN_STEP_REASON_TEXT,
    LABEL_REASON_TEXT,
    MAKE_TARGET_REASON_TEXT,
    WorkflowContractDocsRenderer,
    extract_block_from_content,
)

# ============================================================================
# Fixtures
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录"""
    # tests/ci/test_workflow_contract_docs_generated_blocks.py -> project_root
    return Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def project_root() -> Path:
    """项目根目录 fixture"""
    return get_project_root()


@pytest.fixture
def contract_path(project_root: Path) -> Path:
    """合约文件路径"""
    return project_root / "scripts" / "ci" / "workflow_contract.v1.json"


@pytest.fixture
def contract_md_path(project_root: Path) -> Path:
    """contract.md 文档路径"""
    return project_root / "docs" / "ci_nightly_workflow_refactor" / "contract.md"


@pytest.fixture
def coupling_map_md_path(project_root: Path) -> Path:
    """coupling_map.md 文档路径"""
    return project_root / "docs" / "ci_nightly_workflow_refactor" / "coupling_map.md"


@pytest.fixture
def renderer(contract_path: Path) -> WorkflowContractDocsRenderer:
    """渲染器 fixture"""
    r = WorkflowContractDocsRenderer(contract_path)
    assert r.load_contract(), f"Failed to load contract from {contract_path}"
    return r


@pytest.fixture
def contract_md_content(contract_md_path: Path) -> str:
    """contract.md 文档内容"""
    assert contract_md_path.exists(), f"contract.md not found at {contract_md_path}"
    return contract_md_path.read_text(encoding="utf-8")


@pytest.fixture
def coupling_map_md_content(coupling_map_md_path: Path) -> str:
    """coupling_map.md 文档内容"""
    assert coupling_map_md_path.exists(), f"coupling_map.md not found at {coupling_map_md_path}"
    return coupling_map_md_path.read_text(encoding="utf-8")


def parse_markdown_table_rows(content: str) -> list[list[str]]:
    """解析 Markdown 表格内容为行列表（跳过表头与分隔线）"""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return []

    rows: list[list[str]] = []
    for line in lines[2:]:
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells:
            rows.append(cells)
    return rows


def build_table_row_map(rows: list[list[str]]) -> dict[str, list[str]]:
    """将表格行映射为 {首列值: 行}，并去除首列的反引号"""
    row_map: dict[str, list[str]] = {}
    for row in rows:
        if not row:
            continue
        row_map[row[0].strip("`")] = row
    return row_map


# ============================================================================
# Test: contract.md 受控块
# ============================================================================


class TestContractMdBlocks:
    """测试 contract.md 中的受控块"""

    def test_ci_job_table_matches_rendered(
        self, renderer: WorkflowContractDocsRenderer, contract_md_content: str
    ) -> None:
        """CI_JOB_TABLE 块内容应与渲染结果一致"""
        block_name = "CI_JOB_TABLE"
        rendered = renderer.render_ci_job_table()

        actual_content, begin_line, end_line = extract_block_from_content(
            contract_md_content, block_name
        )

        assert actual_content is not None, (
            f"Block {block_name} not found in contract.md. "
            f"Ensure markers <!-- BEGIN:{block_name} --> and <!-- END:{block_name} --> exist."
        )
        assert begin_line >= 0, f"Invalid begin line for {block_name}"
        assert end_line > begin_line, f"Invalid end line for {block_name}"

        assert actual_content.strip() == rendered.content.strip(), (
            f"Block {block_name} content mismatch.\n"
            f"Run: python scripts/ci/render_workflow_contract_docs.py --block {block_name}\n"
            f"to see expected content."
        )

    def test_nightly_job_table_matches_rendered(
        self, renderer: WorkflowContractDocsRenderer, contract_md_content: str
    ) -> None:
        """NIGHTLY_JOB_TABLE 块内容应与渲染结果一致"""
        block_name = "NIGHTLY_JOB_TABLE"
        rendered = renderer.render_nightly_job_table()

        actual_content, begin_line, end_line = extract_block_from_content(
            contract_md_content, block_name
        )

        assert actual_content is not None, (
            f"Block {block_name} not found in contract.md. Ensure markers exist."
        )

        assert actual_content.strip() == rendered.content.strip(), (
            f"Block {block_name} content mismatch."
        )

    def test_frozen_job_names_table_matches_rendered(
        self, renderer: WorkflowContractDocsRenderer, contract_md_content: str
    ) -> None:
        """FROZEN_JOB_NAMES_TABLE 块内容应与渲染结果一致"""
        block_name = "FROZEN_JOB_NAMES_TABLE"
        rendered = renderer.render_frozen_job_names_table()

        actual_content, begin_line, end_line = extract_block_from_content(
            contract_md_content, block_name
        )

        assert actual_content is not None, (
            f"Block {block_name} not found in contract.md. Ensure markers exist."
        )

        assert actual_content.strip() == rendered.content.strip(), (
            f"Block {block_name} content mismatch."
        )

    def test_frozen_step_names_table_matches_rendered(
        self, renderer: WorkflowContractDocsRenderer, contract_md_content: str
    ) -> None:
        """FROZEN_STEP_NAMES_TABLE 块内容应与渲染结果一致"""
        block_name = "FROZEN_STEP_NAMES_TABLE"
        rendered = renderer.render_frozen_step_names_table()

        actual_content, begin_line, end_line = extract_block_from_content(
            contract_md_content, block_name
        )

        assert actual_content is not None, (
            f"Block {block_name} not found in contract.md. Ensure markers exist."
        )

        assert actual_content.strip() == rendered.content.strip(), (
            f"Block {block_name} content mismatch."
        )

    def test_make_targets_table_matches_rendered(
        self, renderer: WorkflowContractDocsRenderer, contract_md_content: str
    ) -> None:
        """MAKE_TARGETS_TABLE 块内容应与渲染结果一致"""
        block_name = "MAKE_TARGETS_TABLE"
        rendered = renderer.render_make_targets_table()

        actual_content, begin_line, end_line = extract_block_from_content(
            contract_md_content, block_name
        )

        assert actual_content is not None, (
            f"Block {block_name} not found in contract.md. Ensure markers exist."
        )

        assert actual_content.strip() == rendered.content.strip(), (
            f"Block {block_name} content mismatch."
        )

    def test_labels_table_matches_rendered(
        self, renderer: WorkflowContractDocsRenderer, contract_md_content: str
    ) -> None:
        """LABELS_TABLE 块内容应与渲染结果一致"""
        block_name = "LABELS_TABLE"
        rendered = renderer.render_labels_table()

        actual_content, begin_line, end_line = extract_block_from_content(
            contract_md_content, block_name
        )

        assert actual_content is not None, (
            f"Block {block_name} not found in contract.md. Ensure markers exist."
        )

        assert actual_content.strip() == rendered.content.strip(), (
            f"Block {block_name} content mismatch."
        )

    def test_all_contract_blocks_have_markers(self, contract_md_content: str) -> None:
        """所有 contract.md 受控块都应有 markers"""
        for block_name in CONTRACT_BLOCK_NAMES:
            begin_marker = f"<!-- BEGIN:{block_name} -->"
            end_marker = f"<!-- END:{block_name} -->"

            assert begin_marker in contract_md_content, (
                f"Missing BEGIN marker for {block_name} in contract.md"
            )
            assert end_marker in contract_md_content, (
                f"Missing END marker for {block_name} in contract.md"
            )


# ============================================================================
# Test: coupling_map.md 受控块
# ============================================================================


class TestCouplingMapMdBlocks:
    """测试 coupling_map.md 中的受控块"""

    def test_ci_jobs_list_matches_rendered(
        self, renderer: WorkflowContractDocsRenderer, coupling_map_md_content: str
    ) -> None:
        """CI_JOBS_LIST 块内容应与渲染结果一致"""
        block_name = "CI_JOBS_LIST"
        rendered = renderer.render_ci_jobs_list()

        actual_content, begin_line, end_line = extract_block_from_content(
            coupling_map_md_content, block_name
        )

        assert actual_content is not None, (
            f"Block {block_name} not found in coupling_map.md. "
            f"Ensure markers <!-- BEGIN:{block_name} --> and <!-- END:{block_name} --> exist."
        )

        assert actual_content.strip() == rendered.content.strip(), (
            f"Block {block_name} content mismatch.\n"
            f"Run: python scripts/ci/render_workflow_contract_docs.py --block {block_name}\n"
            f"to see expected content."
        )

    def test_nightly_jobs_list_matches_rendered(
        self, renderer: WorkflowContractDocsRenderer, coupling_map_md_content: str
    ) -> None:
        """NIGHTLY_JOBS_LIST 块内容应与渲染结果一致"""
        block_name = "NIGHTLY_JOBS_LIST"
        rendered = renderer.render_nightly_jobs_list()

        actual_content, begin_line, end_line = extract_block_from_content(
            coupling_map_md_content, block_name
        )

        assert actual_content is not None, (
            f"Block {block_name} not found in coupling_map.md. Ensure markers exist."
        )

        assert actual_content.strip() == rendered.content.strip(), (
            f"Block {block_name} content mismatch."
        )

    def test_make_targets_list_matches_rendered(
        self, renderer: WorkflowContractDocsRenderer, coupling_map_md_content: str
    ) -> None:
        """MAKE_TARGETS_LIST 块内容应与渲染结果一致"""
        block_name = "MAKE_TARGETS_LIST"
        rendered = renderer.render_make_targets_list()

        actual_content, begin_line, end_line = extract_block_from_content(
            coupling_map_md_content, block_name
        )

        assert actual_content is not None, (
            f"Block {block_name} not found in coupling_map.md. Ensure markers exist."
        )

        assert actual_content.strip() == rendered.content.strip(), (
            f"Block {block_name} content mismatch."
        )

    def test_all_coupling_map_blocks_have_markers(self, coupling_map_md_content: str) -> None:
        """所有 coupling_map.md 受控块都应有 markers"""
        for block_name in COUPLING_MAP_BLOCK_NAMES:
            begin_marker = f"<!-- BEGIN:{block_name} -->"
            end_marker = f"<!-- END:{block_name} -->"

            assert begin_marker in coupling_map_md_content, (
                f"Missing BEGIN marker for {block_name} in coupling_map.md"
            )
            assert end_marker in coupling_map_md_content, (
                f"Missing END marker for {block_name} in coupling_map.md"
            )


# ============================================================================
# Test: 受控块列来源策略
# ============================================================================


class TestRenderedColumnPolicy:
    """验证受控块中固定文案与合约字段来源策略"""

    def test_job_table_description_uses_contract_comments(
        self, renderer: WorkflowContractDocsRenderer, contract_path: Path
    ) -> None:
        """Job 表说明列优先使用 required_jobs[*]._comment"""
        contract = json.loads(contract_path.read_text(encoding="utf-8"))

        ci_rows = parse_markdown_table_rows(renderer.render_ci_job_table().content)
        ci_row_map = build_table_row_map(ci_rows)
        for job in contract.get("ci", {}).get("required_jobs", []):
            job_id = job.get("id", "")
            comment = job.get("_comment", "")
            if job_id and comment:
                assert ci_row_map[job_id][2] == comment

        nightly_rows = parse_markdown_table_rows(renderer.render_nightly_job_table().content)
        nightly_row_map = build_table_row_map(nightly_rows)
        for job in contract.get("nightly", {}).get("required_jobs", []):
            job_id = job.get("id", "")
            comment = job.get("_comment", "")
            if job_id and comment:
                assert nightly_row_map[job_id][2] == comment

    def test_fixed_reason_columns_use_renderer_constants(
        self, renderer: WorkflowContractDocsRenderer
    ) -> None:
        """固定文案列应来自渲染器常量"""
        frozen_job_rows = parse_markdown_table_rows(
            renderer.render_frozen_job_names_table().content
        )
        assert frozen_job_rows, "Expected frozen job names table to have rows"
        for row in frozen_job_rows:
            assert row[1] == FROZEN_JOB_REASON_TEXT

        frozen_step_rows = parse_markdown_table_rows(
            renderer.render_frozen_step_names_table().content
        )
        assert frozen_step_rows, "Expected frozen step names table to have rows"
        for row in frozen_step_rows:
            assert row[1] == FROZEN_STEP_REASON_TEXT

        make_target_rows = parse_markdown_table_rows(renderer.render_make_targets_table().content)
        assert make_target_rows, "Expected make targets table to have rows"
        for row in make_target_rows:
            assert row[1] == MAKE_TARGET_REASON_TEXT

        label_rows = parse_markdown_table_rows(renderer.render_labels_table().content)
        assert label_rows, "Expected labels table to have rows"
        for row in label_rows:
            assert row[2] == LABEL_REASON_TEXT

        coupling_target_rows = parse_markdown_table_rows(
            renderer.render_make_targets_list().content
        )
        assert coupling_target_rows, "Expected coupling map targets list to have rows"
        for row in coupling_target_rows:
            assert row[1] == COUPLING_MAP_TARGET_REASON_TEXT


# ============================================================================
# Test: 集成测试 - 完整的渲染和比对流程
# ============================================================================


class TestIntegrationRenderedBlocksSync:
    """集成测试：验证所有受控块的端到端一致性"""

    def test_all_contract_blocks_in_sync(
        self, renderer: WorkflowContractDocsRenderer, contract_md_content: str
    ) -> None:
        """所有 contract.md 受控块应与渲染结果同步"""
        blocks = renderer.render_contract_blocks()
        errors: list[str] = []

        for block_name, rendered_block in blocks.items():
            actual_content, begin_line, end_line = extract_block_from_content(
                contract_md_content, block_name
            )

            if actual_content is None:
                errors.append(f"Block {block_name} not found in contract.md")
                continue

            if actual_content.strip() != rendered_block.content.strip():
                errors.append(
                    f"Block {block_name} content mismatch (lines {begin_line + 2}-{end_line})"
                )

        assert len(errors) == 0, (
            f"Found {len(errors)} block sync errors in contract.md:\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\n\nRun: make check-workflow-contract-docs-sync --verbose"
        )

    def test_all_coupling_map_blocks_in_sync(
        self, renderer: WorkflowContractDocsRenderer, coupling_map_md_content: str
    ) -> None:
        """所有 coupling_map.md 受控块应与渲染结果同步"""
        blocks = renderer.render_coupling_map_blocks()
        errors: list[str] = []

        for block_name, rendered_block in blocks.items():
            actual_content, begin_line, end_line = extract_block_from_content(
                coupling_map_md_content, block_name
            )

            if actual_content is None:
                errors.append(f"Block {block_name} not found in coupling_map.md")
                continue

            if actual_content.strip() != rendered_block.content.strip():
                errors.append(
                    f"Block {block_name} content mismatch (lines {begin_line + 2}-{end_line})"
                )

        assert len(errors) == 0, (
            f"Found {len(errors)} block sync errors in coupling_map.md:\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\n\nRun: make check-workflow-contract-coupling-map-sync --verbose"
        )


# ============================================================================
# Test: 渲染器稳定性
# ============================================================================


class TestRendererStability:
    """测试渲染器的稳定性和幂等性"""

    def test_rendering_is_idempotent(self, contract_path: Path) -> None:
        """多次渲染应产生相同结果（幂等性）"""
        results: list[dict[str, str]] = []

        for _ in range(3):
            renderer = WorkflowContractDocsRenderer(contract_path)
            assert renderer.load_contract()
            blocks = renderer.render_all_blocks()
            results.append({name: block.content for name, block in blocks.items()})

        for i in range(1, len(results)):
            assert results[0] == results[i], (
                f"Render {i} differs from render 0. Rendering is not idempotent."
            )

    def test_all_blocks_have_proper_markers(self, renderer: WorkflowContractDocsRenderer) -> None:
        """所有渲染的块应包含正确的 markers"""
        all_blocks = renderer.render_all_blocks()

        for block_name, block in all_blocks.items():
            assert block.begin_marker == f"<!-- BEGIN:{block_name} -->", (
                f"Block {block_name} has incorrect begin marker"
            )
            assert block.end_marker == f"<!-- END:{block_name} -->", (
                f"Block {block_name} has incorrect end marker"
            )

            # full_block 应包含 markers 和内容
            full = block.full_block()
            assert full.startswith(block.begin_marker), (
                f"Block {block_name} full_block should start with begin marker"
            )
            assert full.endswith(block.end_marker), (
                f"Block {block_name} full_block should end with end marker"
            )
            assert block.content in full, f"Block {block_name} content should be in full_block"
