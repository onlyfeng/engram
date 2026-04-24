"""Regression tests for the OpenMemory launcher wrappers."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _copy_openmemory_launcher(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts" / "ops").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "Makefile", repo / "Makefile")
    scripts_dir = REPO_ROOT / "scripts" / "ops"
    repo_scripts_dir = repo / "scripts" / "ops"
    shutil.copy2(
        scripts_dir / "start_openmemory.sh",
        repo_scripts_dir / "start_openmemory.sh",
    )
    shutil.copy2(
        scripts_dir / "start_openmemory_dashboard.sh",
        repo_scripts_dir / "start_openmemory_dashboard.sh",
    )
    shutil.copy2(
        scripts_dir / "load_env_local.sh",
        repo_scripts_dir / "load_env_local.sh",
    )
    os.chmod(
        repo_scripts_dir / "start_openmemory.sh",
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )
    os.chmod(repo_scripts_dir / "start_openmemory_dashboard.sh", stat.S_IRUSR | stat.S_IWUSR)
    os.chmod(
        repo_scripts_dir / "load_env_local.sh",
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )
    return repo


def test_start_openmemory_fails_fast_for_missing_explicit_dir(tmp_path: Path) -> None:
    repo = _copy_openmemory_launcher(tmp_path)
    missing_dir = repo / "does-not-exist" / "packages" / "openmemory-js"

    bash = shutil.which("bash")
    assert bash, "bash required to exercise the launcher script"
    result = subprocess.run(
        [bash, "scripts/ops/start_openmemory.sh", "--openmemory-dir", str(missing_dir)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ["PATH"],
        },
    )

    assert result.returncode != 0
    assert "指定的 OpenMemory 目录不存在" in result.stderr


def test_make_openmemory_preserves_explicit_opm_override(tmp_path: Path) -> None:
    repo = _copy_openmemory_launcher(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_opm = tmp_path / "fake-opm"
    fake_opm.write_text(
        '#!/usr/bin/env bash\nprintf \'fake-opm cwd=%s args=%s\\n\' "$(pwd)" "$*"\n',
        encoding="utf-8",
    )
    os.chmod(fake_opm, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    result = subprocess.run(
        ["make", "--no-print-directory", "openmemory", f"OPM={fake_opm}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={
            "HOME": str(fake_home),
            "PATH": os.environ["PATH"],
        },
    )

    assert result.returncode == 0, result.stderr
    assert "runtime=explicit-opm" in result.stdout
    assert "fake-opm cwd=" in result.stdout
    assert "args=serve" in result.stdout


def test_make_openmemory_dashboard_runs_script_via_bash(tmp_path: Path) -> None:
    repo = _copy_openmemory_launcher(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "openmemory-dashboard",
            "OPENMEMORY_DASHBOARD_DIR=/missing",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={
            "HOME": str(fake_home),
            "PATH": os.environ["PATH"],
        },
    )

    assert result.returncode != 0
    assert "Permission denied" not in result.stderr
    assert "指定的 Dashboard 目录不存在" in result.stderr


def test_dashboard_launcher_resolves_relative_openmemory_dir_env_from_caller_cwd(
    tmp_path: Path,
) -> None:
    """OPENMEMORY_DIR env var with a relative value must be resolved from the caller's CWD.

    Layout:
      tmp_path/caller/           ← caller CWD
      tmp_path/openmemory/dashboard/   ← valid-looking checkout (no package.json)

    OPENMEMORY_DIR=../openmemory is relative to caller_cwd.  The repo root is a
    different directory, so if the script cd-s first and then resolves the relative
    path, it will NOT find the dashboard dir and will fall through to auto-discovery
    ("未找到可启动的 OpenMemory Dashboard").  With the fix the path is resolved from
    caller_cwd first and the script should reach the "缺少 package.json" fast-fail.
    """
    repo = _copy_openmemory_launcher(tmp_path)
    caller_cwd = tmp_path / "caller"
    caller_cwd.mkdir()
    # Create a dashboard dir sibling to caller_cwd — only valid via the relative path
    # from caller_cwd, NOT from the repo root.
    (tmp_path / "openmemory" / "dashboard").mkdir(parents=True)
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = subprocess.run(
        ["bash", str(repo / "scripts" / "ops" / "start_openmemory_dashboard.sh")],
        cwd=caller_cwd,
        capture_output=True,
        text=True,
        check=False,
        env={
            "HOME": str(fake_home),
            "PATH": os.environ["PATH"],
            "OPENMEMORY_DIR": "../openmemory",
        },
    )

    assert result.returncode != 0
    assert "Permission denied" not in result.stderr
    # With the fix, the script resolves OPENMEMORY_DIR from caller_cwd, finds
    # the dashboard/ subdir, and fails fast because package.json is missing.
    # Without the fix it silently falls through and emits "未找到可启动的".
    assert "package.json" in result.stderr
