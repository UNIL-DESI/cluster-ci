"""Tests for runtime environment and executable resolution (Fix 5)."""

import os
import stat
import subprocess
import sys
import pytest

from src.runner.runtime_env import resolve_venv_executable
from src.runner.workspace_sanitizer import WorkspaceSanitizerError, sanitize_workspace


def _create_mock_executable(directory: str, name: str) -> str:
    """Create a mock executable file with cross-platform support."""
    os.makedirs(directory, exist_ok=True)
    if sys.platform.startswith("win"):
        # On Windows, create .cmd or .exe
        target = os.path.join(directory, f"{name}.cmd")
        with open(target, "w", encoding="utf-8") as f:
            f.write("@echo off\nexit 0\n")
    else:
        target = os.path.join(directory, name)
        with open(target, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def test_resolve_venv_executable_found_in_venv_with_empty_path(tmp_path, monkeypatch):
    """Test Case 1: Executable found next to sys.executable even when PATH is completely empty."""
    venv_bin = tmp_path / "venv" / "bin"
    mock_py = venv_bin / ("python.exe" if sys.platform.startswith("win") else "python")
    _create_mock_executable(str(venv_bin), "python")
    _create_mock_executable(str(venv_bin), "dvc")

    monkeypatch.setattr(sys, "executable", str(mock_py))
    monkeypatch.setenv("PATH", "")

    resolved = resolve_venv_executable("dvc")
    assert os.path.isfile(resolved)
    assert os.path.dirname(resolved) == str(venv_bin)


def test_resolve_venv_executable_found_via_path(tmp_path, monkeypatch):
    """Test Case 2: Executable found via system PATH when absent from venv directory."""
    empty_venv = tmp_path / "empty_venv" / "bin"
    empty_venv.mkdir(parents=True)
    mock_py = empty_venv / ("python.exe" if sys.platform.startswith("win") else "python")
    _create_mock_executable(str(empty_venv), "python")

    custom_path = tmp_path / "custom_path"
    _create_mock_executable(str(custom_path), "uv")

    monkeypatch.setattr(sys, "executable", str(mock_py))
    monkeypatch.setenv("PATH", str(custom_path))

    resolved = resolve_venv_executable("uv")
    assert os.path.isfile(resolved)
    assert os.path.dirname(resolved) == str(custom_path)


def test_resolve_venv_executable_missing_raises_explicit_error(tmp_path, monkeypatch):
    """Test Case 3: Missing executable raises explicit FileNotFoundError with diagnostic details."""
    venv_dir = tmp_path / "venv" / "bin"
    venv_dir.mkdir(parents=True)
    mock_py = venv_dir / "python"

    monkeypatch.setattr(sys, "executable", str(mock_py))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    with pytest.raises(FileNotFoundError) as exc_info:
        resolve_venv_executable("nonexistent_binary_xyz_123")

    msg = str(exc_info.value)
    assert "nonexistent_binary_xyz_123" in msg
    assert str(venv_dir) in msg
    assert "/usr/bin:/bin" in msg


def test_resolve_venv_executable_already_absolute_path(tmp_path):
    """Test Case 4: Pre-resolved valid absolute path returns itself."""
    bin_dir = tmp_path / "bin"
    exe = _create_mock_executable(str(bin_dir), "custom_tool")
    resolved = resolve_venv_executable(exe)
    assert os.path.abspath(exe) == resolved


def test_sanitize_workspace_resolves_dvc_without_venv_in_path(tmp_path, monkeypatch):
    """Test Case 5: sanitize_workspace resolves dvc from venv when PATH does not contain venv."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".dvc").mkdir()
    (ws / "dvc.yaml").write_text("stages: {}\n", encoding="utf-8")
    (ws / "dvc.lock").write_text("schema: '2.0'\nstages: {}\n", encoding="utf-8")

    fake_dvc = str(tmp_path / "venv" / "bin" / "dvc")
    monkeypatch.setattr("src.runner.workspace_sanitizer.resolve_venv_executable", lambda name: fake_dvc)

    recorded_cmd = []

    class DummyProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def mock_run(cmd, *args, **kwargs):
        recorded_cmd.append(list(cmd))
        return DummyProc()

    monkeypatch.setattr(subprocess, "run", mock_run)

    res = sanitize_workspace(str(ws), dvc_checkout=True, strict_lock_check=False)
    assert res["workspace"] == str(ws)
    assert recorded_cmd, "subprocess.run should have been called"
    assert recorded_cmd[0][0] == fake_dvc
    assert recorded_cmd[0][1:] == ["checkout", "--force", "--allow-missing"]


def test_sanitize_workspace_fails_explicitly_when_dvc_not_found(tmp_path, monkeypatch):
    """Test Case 6: sanitize_workspace fails fast with WorkspaceSanitizerError if dvc is not found."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".dvc").mkdir()
    (ws / "dvc.yaml").write_text("stages: {}\n", encoding="utf-8")
    (ws / "dvc.lock").write_text("schema: '2.0'\nstages: {}\n", encoding="utf-8")

    def mock_resolve_fail(name):
        raise FileNotFoundError(f"Executable '{name}' not found.")

    monkeypatch.setattr("src.runner.workspace_sanitizer.resolve_venv_executable", mock_resolve_fail)

    with pytest.raises(WorkspaceSanitizerError) as exc_info:
        sanitize_workspace(str(ws), dvc_checkout=True, strict_lock_check=False)

    assert "Cannot perform DVC checkout" in str(exc_info.value)
