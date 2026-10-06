"""Tests for Cluster-CI Workspace Sanitizer (Bug 9)."""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import pytest

from src.runner.workspace_sanitizer import sanitize_workspace


def _remove_readonly(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


@pytest.fixture
def temp_workspace():
    """Create a temporary Git workspace with dummy dvc.lock, cache and residual files."""
    d = tempfile.mkdtemp(prefix="test_sanitizer_ws_")
    try:
        # Initialize dummy git repo
        subprocess.run(["git", "init"], cwd=d, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=d, check=True)

        # Create preserved directories and files
        dvc_cache_dir = os.path.join(d, ".dvc", "cache", "files", "md5")
        os.makedirs(dvc_cache_dir, exist_ok=True)
        with open(os.path.join(dvc_cache_dir, "cache_block.dat"), "w") as f:
            f.write("important cached data block")

        venv_dir = os.path.join(d, "venv", "bin")
        os.makedirs(venv_dir, exist_ok=True)
        with open(os.path.join(venv_dir, "activate"), "w") as f:
            f.write("# venv shim")

        with open(os.path.join(d, ".env"), "w") as f:
            f.write("SECRET_KEY=12345\n")

        with open(os.path.join(d, ".cluster-ci"), "w") as f:
            f.write("REQUIRED_RAM=16GB\n")

        # Create valid output matching dvc.lock
        data_dir = os.path.join(d, "data")
        os.makedirs(data_dir, exist_ok=True)
        valid_bytes = b"valid dataset content\n"
        valid_md5 = hashlib.md5(valid_bytes).hexdigest()
        valid_file = os.path.join(data_dir, "clean_data.csv")
        with open(valid_file, "wb") as f:
            f.write(valid_bytes)

        # Write dvc.lock
        dvc_lock = f"""schema: '2.0'
stages:
  prep:
    cmd: python prep.py
    outs:
      - path: data/clean_data.csv
        md5: {valid_md5}
"""
        with open(os.path.join(d, "dvc.lock"), "w", encoding="utf-8") as f:
            f.write(dvc_lock)

        # Git commit clean base
        with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(".dvc/cache\nvenv\n")
        subprocess.run(["git", "add", "."], cwd=d, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial clean state"], cwd=d, check=True, capture_output=True)

        yield d, valid_md5
    finally:
        shutil.rmtree(d, onerror=_remove_readonly, ignore_errors=True)


def test_sanitize_purges_stale_files_and_preserves_cache(temp_workspace):
    """Test Bug 9: stale files are purged, .dvc/cache, venv and .env are strictly preserved."""
    ws, _ = temp_workspace

    # Plant stale residual files
    stale_file1 = os.path.join(ws, "data", "leftover.stale_20261005")
    stale_file2 = os.path.join(ws, ".stale_temp_lock")
    with open(stale_file1, "w") as f:
        f.write("stale data leak")
    with open(stale_file2, "w") as f:
        f.write("stale lock")

    # Plant an untracked garbage file
    garbage_file = os.path.join(ws, "garbage_dump.tmp")
    with open(garbage_file, "w") as f:
        f.write("untracked garbage")

    res = sanitize_workspace(ws, dvc_checkout=False, strict_lock_check=True)
    assert len(res["purged_files"]) >= 3

    # Stale files must be gone
    assert not os.path.exists(stale_file1)
    assert not os.path.exists(stale_file2)
    assert not os.path.exists(garbage_file)

    # Preserved paths must remain intact
    assert os.path.exists(os.path.join(ws, ".dvc", "cache", "files", "md5", "cache_block.dat"))
    assert os.path.exists(os.path.join(ws, "venv", "bin", "activate"))
    assert os.path.exists(os.path.join(ws, ".env"))
    assert os.path.exists(os.path.join(ws, ".cluster-ci"))
    assert os.path.exists(os.path.join(ws, "data", "clean_data.csv"))


def test_sanitize_detects_hash_mismatch_against_dvc_lock(temp_workspace):
    """Test Bug 9: corrupted or modified output whose hash mismatches dvc.lock raises RuntimeError."""
    ws, _ = temp_workspace

    # Corrupt the valid output
    corrupt_file = os.path.join(ws, "data", "clean_data.csv")
    with open(corrupt_file, "w", encoding="utf-8") as f:
        f.write("corrupted or leaked test pairs from previous run\n")

    with pytest.raises(RuntimeError) as excinfo:
        sanitize_workspace(ws, dvc_checkout=False, strict_lock_check=True)

    err = str(excinfo.value)
    assert "Écart d'intégrité détecté" in err
    assert "data/clean_data.csv" in err
    assert "attendu" in err


def test_sanitize_safety_guard_rejects_unsafe_workspace():
    """Test Bug 9: safety invariant prevents running on root filesystem."""
    with pytest.raises(ValueError) as excinfo:
        sanitize_workspace("/", dvc_checkout=False, strict_lock_check=False)
    assert "Safety violation" in str(excinfo.value)


def test_sanitize_cli_entrypoint(temp_workspace):
    """Test Bug 9: CLI entrypoint executes cleanly and prints verification report."""
    ws, _ = temp_workspace
    cmd = [sys.executable, "-m", "src.runner.workspace_sanitizer", ws, "--no-checkout"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "Workspace" in proc.stdout
    assert "is clean and verified" in proc.stdout
