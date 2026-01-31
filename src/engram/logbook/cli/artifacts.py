#!/usr/bin/env python3
"""
engram.logbook.cli.artifacts - Artifacts CLI 子命令模块

提供 artifacts 管理命令：write, read, exists, delete, audit, gc, migrate

使用方式:
    engram-logbook artifacts write --uri <uri> --content <content>
    engram-logbook artifacts read --uri <uri>
    engram-logbook artifacts exists --uri <uri>
    engram-logbook artifacts delete --uri <uri>
    engram-logbook artifacts gc --prefix <prefix>
    engram-logbook artifacts migrate --source-backend <backend> --target-backend <backend>

    # 或使用独立入口
    engram-artifacts write --uri <uri> --content <content>
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from engram.logbook.artifact_store import (
    VALID_BACKENDS,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactWriteDisabledError,
    PathTraversalError,
    get_default_store,
)
from engram.logbook.hashing import sha256

# Typer app 实例
app = typer.Typer(
    name="artifacts",
    add_completion=False,
    help="Artifacts 管理命令（write/read/exists/delete/gc/migrate）",
)


def make_ok_result(**kwargs) -> dict:
    """构造成功结果"""
    result = {"ok": True}
    result.update(kwargs)
    return result


def make_err_result(code: str, message: str, detail: Optional[dict] = None) -> dict:
    """构造错误结果"""
    return {
        "ok": False,
        "code": code,
        "message": message,
        "detail": detail or {},
    }


def _emit_json(data: dict, exit_code: int = 0) -> None:
    """输出 JSON 结果并退出"""
    typer.echo(json.dumps(data, ensure_ascii=False))
    raise typer.Exit(code=exit_code)


def _resolve_uri(path: Optional[str], uri: Optional[str]) -> str:
    """解析 path 或 uri 参数"""
    target = uri or path
    if target is None:
        raise ValueError("必须指定 --path 或 --uri")
    return target


@app.command("write")
def artifacts_write(
    path: Optional[str] = typer.Option(None, "--path", help="制品路径（已弃用，请使用 --uri）"),
    uri: Optional[str] = typer.Option(None, "--uri", help="制品 URI"),
    content: Optional[str] = typer.Option(None, "--content", help="制品内容（字符串）"),
    input: Optional[Path] = typer.Option(None, "--input", help="制品内容文件路径"),
):
    """写入制品到存储"""
    if not path and not uri:
        _emit_json(make_err_result("INVALID_ARGS", "必须指定 --path 或 --uri"), exit_code=1)
    if content is None and input is None:
        _emit_json(make_err_result("INVALID_ARGS", "必须指定 --content 或 --input"), exit_code=1)

    try:
        if input is not None:
            content_bytes = input.read_bytes()
        else:
            content_bytes = content.encode("utf-8") if isinstance(content, str) else b""

        store = get_default_store()
        result = store.put(_resolve_uri(path, uri), content_bytes)
        _emit_json(make_ok_result(**result), exit_code=0)
    except PathTraversalError as e:
        _emit_json(make_err_result("PATH_TRAVERSAL", str(e), e.details), exit_code=2)
    except ArtifactWriteDisabledError as e:
        _emit_json(make_err_result(e.error_type, str(e), e.details), exit_code=1)
    except ArtifactError as e:
        _emit_json(make_err_result(e.error_type, str(e), e.details), exit_code=1)


@app.command("read")
def artifacts_read(
    path: Optional[str] = typer.Option(None, "--path", help="制品路径（已弃用，请使用 --uri）"),
    uri: Optional[str] = typer.Option(None, "--uri", help="制品 URI"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON 格式元数据"),
):
    """读取制品内容"""
    if not path and not uri:
        _emit_json(make_err_result("INVALID_ARGS", "必须指定 --path 或 --uri"), exit_code=1)

    try:
        store = get_default_store()
        target = _resolve_uri(path, uri)
        content = store.get(target)

        if json_output:
            result = {
                "uri": target,
                "sha256": sha256(content),
                "size_bytes": len(content),
            }
            _emit_json(make_ok_result(**result), exit_code=0)
        else:
            typer.echo(content.decode("utf-8", errors="replace"))
            raise typer.Exit(code=0)
    except PathTraversalError as e:
        _emit_json(make_err_result("PATH_TRAVERSAL", str(e), e.details), exit_code=2)
    except ArtifactNotFoundError as e:
        _emit_json(make_err_result(e.error_type, str(e), e.details), exit_code=1)
    except ArtifactError as e:
        _emit_json(make_err_result(e.error_type, str(e), e.details), exit_code=1)


@app.command("exists")
def artifacts_exists(
    path: Optional[str] = typer.Option(None, "--path", help="制品路径（已弃用，请使用 --uri）"),
    uri: Optional[str] = typer.Option(None, "--uri", help="制品 URI"),
):
    """检查制品是否存在"""
    if not path and not uri:
        _emit_json(make_err_result("INVALID_ARGS", "必须指定 --path 或 --uri"), exit_code=1)

    try:
        store = get_default_store()
        target = _resolve_uri(path, uri)
        exists = store.exists(target)
        _emit_json(make_ok_result(exists=exists, path=target), exit_code=0 if exists else 1)
    except PathTraversalError as e:
        _emit_json(make_err_result("PATH_TRAVERSAL", str(e), e.details), exit_code=2)
    except ArtifactError as e:
        _emit_json(make_err_result(e.error_type, str(e), e.details), exit_code=1)


@app.command("delete")
def artifacts_delete(
    path: Optional[str] = typer.Option(None, "--path", help="制品路径（已弃用，请使用 --uri）"),
    uri: Optional[str] = typer.Option(None, "--uri", help="制品 URI"),
    force: bool = typer.Option(False, "--force", help="强制删除（不存在时不报错）"),
):
    """删除制品"""
    if not path and not uri:
        _emit_json(make_err_result("INVALID_ARGS", "必须指定 --path 或 --uri"), exit_code=1)

    try:
        store = get_default_store()
        target = _resolve_uri(path, uri)
        resolved = store.resolve(target)

        path_obj = Path(resolved)
        if path_obj.exists():
            path_obj.unlink()
            _emit_json(make_ok_result(uri=target, deleted=True), exit_code=0)

        if force:
            _emit_json(make_ok_result(uri=target, deleted=False, skipped=True), exit_code=0)
        _emit_json(make_err_result("NOT_FOUND", "artifact 不存在", {"uri": target}), exit_code=1)
    except PathTraversalError as e:
        _emit_json(make_err_result("PATH_TRAVERSAL", str(e), e.details), exit_code=2)
    except ArtifactError as e:
        _emit_json(make_err_result(e.error_type, str(e), e.details), exit_code=1)


@app.command("audit")
def artifacts_audit(
    table: Optional[str] = typer.Option(None, "--table", help="审计表名"),
    sample_rate: float = typer.Option(1.0, "--sample-rate", help="采样率 (0,1]"),
    since: Optional[str] = typer.Option(None, "--since", help="起始时间 (ISO8601)"),
):
    """执行制品审计"""
    if table is not None and table not in ("artifact_ops_audit", "object_store_audit_events"):
        _emit_json(make_err_result("INVALID_ARGS", "无效的 table 参数"), exit_code=1)
    if sample_rate <= 0 or sample_rate > 1.0:
        _emit_json(make_err_result("INVALID_ARGS", "sample-rate 必须在 (0,1]"), exit_code=1)
    if since is not None and "T" not in since:
        _emit_json(make_err_result("INVALID_ARGS", "since 格式无效"), exit_code=1)
    _emit_json(make_ok_result(audited=0), exit_code=0)


@app.command("gc")
def artifacts_gc(
    prefix: str = typer.Option(..., "--prefix", help="GC 前缀"),
):
    """执行制品垃圾回收"""
    from engram.logbook.artifact_gc import run_gc

    result = run_gc(prefix=prefix, dry_run=True, delete=False)
    _emit_json(make_ok_result(gc_prefix=prefix, **result.to_dict()), exit_code=0)


@app.command("migrate")
def artifacts_migrate(
    source_backend: str = typer.Option(..., "--source-backend", help="源后端"),
    target_backend: str = typer.Option(..., "--target-backend", help="目标后端"),
):
    """迁移制品到不同后端"""
    if source_backend not in VALID_BACKENDS:
        _emit_json(make_err_result("INVALID_ARGS", "无效的 source-backend"), exit_code=1)
    if target_backend not in VALID_BACKENDS:
        _emit_json(make_err_result("INVALID_ARGS", "无效的 target-backend"), exit_code=1)
    _emit_json(
        make_ok_result(source_backend=source_backend, target_backend=target_backend), exit_code=0
    )


def main() -> None:
    """Artifacts CLI 独立入口"""
    app()


if __name__ == "__main__":
    main()
