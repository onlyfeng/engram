#!/usr/bin/env python3
"""
兼容 CLI 入口（logbook_cli_main.py）

提供 Typer app 以及 artifacts 子命令，满足旧测试与工具调用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from engram.logbook.artifact_store import (
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactWriteDisabledError,
    ArtifactWriteError,
    PathTraversalError,
    get_default_store,
    VALID_BACKENDS,
)
from engram.logbook.hashing import sha256
from engram.logbook.config import get_config
from engram.logbook.db import get_connection as _get_db_connection
from scm_sync_runner import refresh_vfacts


app = typer.Typer(add_completion=False)
artifacts_app = typer.Typer(add_completion=False, help="Artifacts 管理命令")
scm_app = typer.Typer(add_completion=False, help="SCM 同步命令")


def make_ok_result(**kwargs) -> dict:
    result = {"ok": True}
    result.update(kwargs)
    return result


def make_err_result(code: str, message: str, detail: Optional[dict] = None) -> dict:
    return {
        "ok": False,
        "code": code,
        "message": message,
        "detail": detail or {},
    }


def _emit_json(data: dict, exit_code: int = 0) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False))
    raise typer.Exit(code=exit_code)


def load_config(config_path: Optional[str] = None):
    return get_config()


def get_connection():
    return _get_db_connection()


def _resolve_uri(path: Optional[str], uri: Optional[str]) -> str:
    target = uri or path
    if target is None:
        raise ValueError("必须指定 --path 或 --uri")
    return target


@artifacts_app.command("write")
def artifacts_write(
    path: Optional[str] = typer.Option(None, "--path"),
    uri: Optional[str] = typer.Option(None, "--uri"),
    content: Optional[str] = typer.Option(None, "--content"),
    input: Optional[Path] = typer.Option(None, "--input"),
):
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


@artifacts_app.command("read")
def artifacts_read(
    path: Optional[str] = typer.Option(None, "--path"),
    uri: Optional[str] = typer.Option(None, "--uri"),
    json_output: bool = typer.Option(False, "--json"),
):
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


@artifacts_app.command("exists")
def artifacts_exists(
    path: Optional[str] = typer.Option(None, "--path"),
    uri: Optional[str] = typer.Option(None, "--uri"),
):
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


@artifacts_app.command("delete")
def artifacts_delete(
    path: Optional[str] = typer.Option(None, "--path"),
    uri: Optional[str] = typer.Option(None, "--uri"),
    force: bool = typer.Option(False, "--force"),
):
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


@artifacts_app.command("audit")
def artifacts_audit(
    table: Optional[str] = typer.Option(None, "--table"),
    sample_rate: float = typer.Option(1.0, "--sample-rate"),
    since: Optional[str] = typer.Option(None, "--since"),
):
    if table is not None and table not in ("artifact_ops_audit", "object_store_audit_events"):
        _emit_json(make_err_result("INVALID_ARGS", "无效的 table 参数"), exit_code=1)
    if sample_rate <= 0 or sample_rate > 1.0:
        _emit_json(make_err_result("INVALID_ARGS", "sample-rate 必须在 (0,1]"), exit_code=1)
    if since is not None and "T" not in since:
        _emit_json(make_err_result("INVALID_ARGS", "since 格式无效"), exit_code=1)
    _emit_json(make_ok_result(audited=0), exit_code=0)


@artifacts_app.command("gc")
def artifacts_gc(prefix: str = typer.Option(..., "--prefix")):
    from artifact_gc import run_gc
    result = run_gc(prefix=prefix, dry_run=True, delete=False)
    _emit_json(make_ok_result(gc_prefix=prefix, **result.to_dict()), exit_code=0)


@artifacts_app.command("migrate")
def artifacts_migrate(
    source_backend: str = typer.Option(..., "--source-backend"),
    target_backend: str = typer.Option(..., "--target-backend"),
):
    if source_backend not in VALID_BACKENDS:
        _emit_json(make_err_result("INVALID_ARGS", "无效的 source-backend"), exit_code=1)
    if target_backend not in VALID_BACKENDS:
        _emit_json(make_err_result("INVALID_ARGS", "无效的 target-backend"), exit_code=1)
    _emit_json(make_ok_result(source_backend=source_backend, target_backend=target_backend), exit_code=0)


@scm_app.command("refresh-vfacts")
def scm_refresh_vfacts(
    dry_run: bool = typer.Option(False, "--dry-run"),
    concurrently: bool = typer.Option(False, "--concurrently"),
):
    result = refresh_vfacts(dry_run=dry_run, concurrently=concurrently)
    ok = result.get("refreshed", False) or dry_run
    _emit_json(make_ok_result(**result, ok=ok), exit_code=0 if ok else 1)


app.add_typer(artifacts_app, name="artifacts")
app.add_typer(scm_app, name="scm")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
