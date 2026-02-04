#!/usr/bin/env python3
"""
Generate Workflow Contract Snapshot

读取 .github/workflows/*.yml，输出候选 JSON（每个 job 的 steps 列表、outputs 列表、job name 等）。
输出支持 diff 友好（稳定排序、可选只生成某个 workflow）。

用法:
    # 生成所有 workflow 的快照
    python scripts/ci/generate_workflow_contract_snapshot.py

    # 只生成 ci.yml 的快照
    python scripts/ci/generate_workflow_contract_snapshot.py --workflow ci

    # 输出到文件
    python scripts/ci/generate_workflow_contract_snapshot.py --output snapshot.json

    # 变更前后快照（推荐保存到 artifacts，便于 PR 评审）
    python scripts/ci/generate_workflow_contract_snapshot.py --output artifacts/workflow_snapshot_before.json
    python scripts/ci/generate_workflow_contract_snapshot.py --output artifacts/workflow_snapshot_after.json

    # 包含完整 step 内容（默认只提取 name）
    python scripts/ci/generate_workflow_contract_snapshot.py --include-step-details
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_yaml():
    """尝试加载 yaml 模块，提供友好的错误提示。"""
    try:
        import yaml
        return yaml
    except ImportError:
        print("错误: 需要安装 pyyaml 模块", file=sys.stderr)
        print("  pip install pyyaml", file=sys.stderr)
        sys.exit(1)


def find_workflows_dir() -> Path:
    """查找 .github/workflows 目录。"""
    # 从脚本位置向上查找
    script_dir = Path(__file__).resolve().parent

    # 尝试从 scripts/ci 向上两级找到项目根目录
    for parent in [script_dir.parent.parent, Path.cwd()]:
        workflows_dir = parent / ".github" / "workflows"
        if workflows_dir.is_dir():
            return workflows_dir

    # 也尝试当前目录
    cwd_workflows = Path.cwd() / ".github" / "workflows"
    if cwd_workflows.is_dir():
        return cwd_workflows

    raise FileNotFoundError(
        "无法找到 .github/workflows 目录。"
        "请在项目根目录运行此脚本，或确保 .github/workflows 存在。"
    )


def extract_step_info(step: dict, include_details: bool = False) -> dict:
    """提取 step 的关键信息。"""
    info = {}

    # 基本信息
    if "name" in step:
        info["name"] = step["name"]

    if "id" in step:
        info["id"] = step["id"]

    if include_details:
        # 包含更多细节
        if "uses" in step:
            info["uses"] = step["uses"]

        if "run" in step:
            # 只保留 run 命令的前 100 字符作为预览
            run_content = step["run"]
            if isinstance(run_content, str):
                if len(run_content) > 100:
                    info["run_preview"] = run_content[:100] + "..."
                else:
                    info["run_preview"] = run_content

        if "if" in step:
            info["if"] = step["if"]

        if "env" in step:
            info["env_keys"] = sorted(step["env"].keys()) if isinstance(step["env"], dict) else []

    return info


def extract_job_info(job_id: str, job: dict, include_details: bool = False) -> dict:
    """提取 job 的关键信息。"""
    info = {
        "id": job_id,
        "name": job.get("name", job_id),
    }

    # 提取 outputs
    if "outputs" in job:
        outputs = job["outputs"]
        if isinstance(outputs, dict):
            info["outputs"] = sorted(outputs.keys())
        else:
            info["outputs"] = []
    else:
        info["outputs"] = []

    # 提取 needs 依赖
    if "needs" in job:
        needs = job["needs"]
        if isinstance(needs, list):
            info["needs"] = sorted(needs)
        elif isinstance(needs, str):
            info["needs"] = [needs]
        else:
            info["needs"] = []
    else:
        info["needs"] = []

    # 提取 if 条件
    if "if" in job:
        info["if"] = job["if"]

    # 提取 steps
    steps = job.get("steps", [])
    info["steps"] = []
    for step in steps:
        step_info = extract_step_info(step, include_details)
        if step_info:  # 只添加有内容的 step
            info["steps"].append(step_info)

    # 统计
    info["step_count"] = len(steps)

    # 提取 timeout-minutes
    if "timeout-minutes" in job:
        info["timeout_minutes"] = job["timeout-minutes"]

    # 提取 runs-on
    if "runs-on" in job:
        info["runs_on"] = job["runs-on"]

    return info


def extract_workflow_info(
    workflow_path: Path,
    yaml_module: Any,
    include_details: bool = False
) -> dict:
    """提取单个 workflow 文件的信息。"""
    with open(workflow_path, "r", encoding="utf-8") as f:
        workflow = yaml_module.safe_load(f)

    if not workflow:
        return {"error": "Empty or invalid workflow file"}

    info = {
        "file": str(workflow_path.name),
        "name": workflow.get("name", workflow_path.stem),
    }

    # 提取 on 触发条件
    on_triggers = workflow.get("on", {})
    if isinstance(on_triggers, dict):
        info["triggers"] = sorted(on_triggers.keys())
    elif isinstance(on_triggers, list):
        info["triggers"] = sorted(on_triggers)
    elif isinstance(on_triggers, str):
        info["triggers"] = [on_triggers]
    else:
        info["triggers"] = []

    # 提取 workflow_dispatch inputs
    if isinstance(on_triggers, dict) and "workflow_dispatch" in on_triggers:
        wd = on_triggers["workflow_dispatch"]
        if isinstance(wd, dict) and "inputs" in wd:
            inputs = wd["inputs"]
            if isinstance(inputs, dict):
                info["dispatch_inputs"] = sorted(inputs.keys())

    # 提取全局环境变量
    if "env" in workflow:
        env = workflow["env"]
        if isinstance(env, dict):
            info["global_env_keys"] = sorted(env.keys())

    # 提取 jobs
    jobs = workflow.get("jobs", {})
    info["job_ids"] = sorted(jobs.keys())
    info["job_count"] = len(jobs)

    # 提取每个 job 的详细信息
    info["jobs"] = []
    for job_id in sorted(jobs.keys()):
        job = jobs[job_id]
        job_info = extract_job_info(job_id, job, include_details)
        info["jobs"].append(job_info)

    # 提取 job names 列表（方便对比）
    info["job_names"] = [j["name"] for j in info["jobs"]]

    return info


def generate_snapshot(
    workflows_dir: Path,
    workflow_filter: str | None = None,
    include_details: bool = False
) -> dict:
    """生成 workflow 快照。"""
    yaml = load_yaml()

    snapshot = {
        "_metadata": {
            "generator": "generate_workflow_contract_snapshot.py",
            "workflows_dir": str(workflows_dir),
            "workflow_filter": workflow_filter,
            "include_details": include_details,
        },
        "workflows": {}
    }

    # 查找 workflow 文件
    workflow_files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))

    if not workflow_files:
        snapshot["_metadata"]["warning"] = "No workflow files found"
        return snapshot

    for wf_path in workflow_files:
        wf_name = wf_path.stem

        # 应用过滤
        if workflow_filter and wf_name != workflow_filter:
            continue

        try:
            wf_info = extract_workflow_info(wf_path, yaml, include_details)
            snapshot["workflows"][wf_name] = wf_info
        except Exception as e:
            snapshot["workflows"][wf_name] = {
                "error": str(e),
                "file": wf_path.name
            }

    # 添加汇总统计
    snapshot["_summary"] = {
        "workflow_count": len(snapshot["workflows"]),
        "total_jobs": sum(
            w.get("job_count", 0)
            for w in snapshot["workflows"].values()
            if "error" not in w
        ),
        "workflows_with_errors": [
            name for name, w in snapshot["workflows"].items()
            if "error" in w
        ]
    }

    return snapshot


def main():
    parser = argparse.ArgumentParser(
        description="生成 GitHub Actions workflow 的结构化快照（JSON 格式）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 生成所有 workflow 的快照
  python scripts/ci/generate_workflow_contract_snapshot.py

  # 只生成 ci workflow 的快照
  python scripts/ci/generate_workflow_contract_snapshot.py --workflow ci

  # 输出到文件
  python scripts/ci/generate_workflow_contract_snapshot.py --output snapshot.json

  # 变更前后快照（推荐保存到 artifacts）
  python scripts/ci/generate_workflow_contract_snapshot.py --output artifacts/workflow_snapshot_before.json
  python scripts/ci/generate_workflow_contract_snapshot.py --output artifacts/workflow_snapshot_after.json

  # 包含 step 详细信息
  python scripts/ci/generate_workflow_contract_snapshot.py --include-step-details

用途:
  在修改 workflow 前后分别运行此脚本，然后对比 JSON 差异，
  确保所有变更都被正确反映到 workflow_contract.v2.json 中。
"""
    )

    parser.add_argument(
        "--workflow", "-w",
        type=str,
        default=None,
        help="只生成指定 workflow 的快照（如: ci, nightly, release）"
    )

    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="输出到指定文件（默认输出到 stdout）"
    )

    parser.add_argument(
        "--include-step-details", "-d",
        action="store_true",
        default=False,
        help="包含 step 的详细信息（uses, run preview, if 条件等）"
    )

    parser.add_argument(
        "--workflows-dir",
        type=str,
        default=None,
        help="指定 workflows 目录路径（默认自动查找 .github/workflows）"
    )

    parser.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="使用紧凑 JSON 格式（无缩进）"
    )

    args = parser.parse_args()

    # 确定 workflows 目录
    if args.workflows_dir:
        workflows_dir = Path(args.workflows_dir)
        if not workflows_dir.is_dir():
            print(f"错误: 指定的目录不存在: {workflows_dir}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            workflows_dir = find_workflows_dir()
        except FileNotFoundError as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)

    # 生成快照
    snapshot = generate_snapshot(
        workflows_dir,
        workflow_filter=args.workflow,
        include_details=args.include_step_details
    )

    # 输出 JSON
    indent = None if args.compact else 2
    json_output = json.dumps(snapshot, indent=indent, ensure_ascii=False, sort_keys=True)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(json_output)
            f.write("\n")
        print(f"快照已保存到: {output_path}", file=sys.stderr)
    else:
        print(json_output)

    # 返回是否有错误
    if snapshot.get("_summary", {}).get("workflows_with_errors"):
        sys.exit(1)


if __name__ == "__main__":
    main()
