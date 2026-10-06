"""Tests for Cluster-CI Workspace Sanitizer (Bug 9)."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import pytest

from src.runner.workspace_sanitizer import WorkspaceSanitizerError, sanitize_workspace


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

        # Write dvc.yaml
        dvc_yaml = """schema: '2.0'
stages:
  prep:
    cmd: python prep.py
    outs:
      - data/clean_data.csv
"""
        with open(os.path.join(d, "dvc.yaml"), "w", encoding="utf-8") as f:
            f.write(dvc_yaml)

        yield d, valid_md5
    finally:
        shutil.rmtree(d, onerror=_remove_readonly, ignore_errors=True)


def test_sanitize_preserves_config_local_and_ignored_non_dvc_files(temp_workspace):
    """Test Bug 9 (Point 1): .dvc/config.local, .dvc/tmp, external models, secrets and markers are strictly preserved."""
    ws, _ = temp_workspace

    # Plant files that MUST NEVER be deleted by sanitizer
    config_local = os.path.join(ws, ".dvc", "config.local")
    os.makedirs(os.path.dirname(config_local), exist_ok=True)
    with open(config_local, "w") as f:
        f.write("[core]\nremote = cluster_remote\n")

    dvc_tmp = os.path.join(ws, ".dvc", "tmp", "lock.pid")
    os.makedirs(os.path.dirname(dvc_tmp), exist_ok=True)
    with open(dvc_tmp, "w") as f:
        f.write("12345")

    models_dir = os.path.join(ws, "models_cache", "bert")
    os.makedirs(models_dir, exist_ok=True)
    weights_file = os.path.join(models_dir, "pytorch_model.bin")
    with open(weights_file, "wb") as f:
        f.write(b"model weights outside dvc")

    secret_env = os.path.join(ws, "job_secrets_123.env")
    with open(secret_env, "w") as f:
        f.write("CLUSTER_TOKEN=secret\n")

    guard_marker = os.path.join(ws, "host_guard_killed.marker")
    with open(guard_marker, "w") as f:
        f.write("KILLED_AT=20261006\n")

    # Also plant a genuine stale residual file
    stale_file = os.path.join(ws, ".stale_execution_dump")
    with open(stale_file, "w") as f:
        f.write("stale residue")

    res = sanitize_workspace(ws, dvc_checkout=False, strict_lock_check=True)

    # Stale file must be purged
    assert not os.path.exists(stale_file)
    assert ".stale_execution_dump" in res["purged_files"]

    # All non-DVC files must be preserved
    assert os.path.isfile(config_local)
    assert os.path.isfile(dvc_tmp)
    assert os.path.isfile(weights_file)
    assert os.path.isfile(secret_env)
    assert os.path.isfile(guard_marker)


def test_sanitize_cleans_stray_file_in_directory_output(temp_workspace):
    """Test Bug 9 (Point 1 & 4): stray files inside a declared directory output are purged."""
    ws, _ = temp_workspace

    # Setup directory output with .dir manifest in cache
    dir_out_rel = "data/raw_dir"
    dir_out_abs = os.path.join(ws, "data", "raw_dir")
    os.makedirs(dir_out_abs, exist_ok=True)

    file1_content = b"file 1 content\n"
    file1_md5 = hashlib.md5(file1_content).hexdigest()
    with open(os.path.join(dir_out_abs, "f1.txt"), "wb") as f:
        f.write(file1_content)

    dir_md5 = "ab1234567890abcdef1234567890abcd.dir"
    dir_manifest = [{"md5": file1_md5, "relpath": "f1.txt"}]

    cache_dir = os.path.join(ws, ".dvc", "cache", "files", "md5", dir_md5[:2])
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, dir_md5[2:]), "w", encoding="utf-8") as f:
        json.dump(dir_manifest, f)

    # Update dvc.lock to track directory output
    dvc_lock = f"""schema: '2.0'
stages:
  ingest:
    cmd: python ingest.py
    outs:
      - path: {dir_out_rel}
        md5: {dir_md5}
"""
    with open(os.path.join(ws, "dvc.lock"), "w", encoding="utf-8") as f:
        f.write(dvc_lock)

    # Plant a stray file inside raw_dir not in manifest
    stray_file = os.path.join(dir_out_abs, "stray_leak.txt")
    with open(stray_file, "w") as f:
        f.write("unauthorized stray file")

    res = sanitize_workspace(ws, dvc_checkout=False, strict_lock_check=True)

    # Stray file inside DVC directory output must be removed
    assert not os.path.exists(stray_file)
    assert os.path.isfile(os.path.join(dir_out_abs, "f1.txt"))
    assert any("stray_leak.txt" in p for p in res["purged_files"])


def test_sanitize_healthy_directory_output_no_false_positive(temp_workspace):
    """Test Bug 9 (Point 3 & 4): healthy directory output audited via .dir manifest produces zero false positive."""
    ws, _ = temp_workspace

    dir_out_rel = "data/clean_dir"
    dir_out_abs = os.path.join(ws, "data", "clean_dir")
    os.makedirs(dir_out_abs, exist_ok=True)

    file_a_content = b"content a\n"
    file_a_md5 = hashlib.md5(file_a_content).hexdigest()
    with open(os.path.join(dir_out_abs, "a.csv"), "wb") as f:
        f.write(file_a_content)

    dir_md5 = "fe9876543210fedcba9876543210fedc.dir"
    dir_manifest = [{"md5": file_a_md5, "relpath": "a.csv"}]

    cache_dir = os.path.join(ws, ".dvc", "cache", "files", "md5", dir_md5[:2])
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, dir_md5[2:]), "w", encoding="utf-8") as f:
        json.dump(dir_manifest, f)

    dvc_lock = f"""schema: '2.0'
stages:
  process:
    cmd: python process.py
    outs:
      - path: {dir_out_rel}
        md5: {dir_md5}
"""
    with open(os.path.join(ws, "dvc.lock"), "w", encoding="utf-8") as f:
        f.write(dvc_lock)

    # Sanitizer must succeed without any error or false positive
    res = sanitize_workspace(ws, dvc_checkout=False, strict_lock_check=True)
    assert res["checked_outputs"] >= 1
    assert len(res["mismatches"]) == 0


def test_sanitize_checkout_failure_raises_exception(temp_workspace, monkeypatch):
    """Test Bug 9 (Point 2 & 4): dvc checkout --force failure raises explicit WorkspaceSanitizerError."""
    ws, _ = temp_workspace

    class DummyFailedProcess:
        returncode = 1
        stdout = ""
        stderr = "ERROR: failed to connect to remote storage"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: DummyFailedProcess())

    with pytest.raises(WorkspaceSanitizerError) as excinfo:
        sanitize_workspace(ws, dvc_checkout=True, strict_lock_check=False)

    err = str(excinfo.value)
    assert "DVC checkout failed (exit code 1)" in err
    assert "failed to connect to remote storage" in err


def test_sanitize_detects_hash_mismatch_and_fails_fast(temp_workspace):
    """Test Bug 9 (Point 2): corrupted output raises explicit WorkspaceSanitizerError."""
    ws, _ = temp_workspace

    # Corrupt the valid output
    corrupt_file = os.path.join(ws, "data", "clean_data.csv")
    with open(corrupt_file, "w", encoding="utf-8") as f:
        f.write("corrupted data content\n")

    with pytest.raises(WorkspaceSanitizerError) as excinfo:
        sanitize_workspace(ws, dvc_checkout=False, strict_lock_check=True)

    err = str(excinfo.value)
    assert "Écart d'intégrité détecté" in err
    assert "data/clean_data.csv" in err
    assert "attendu" in err


def test_sanitize_preserves_cache_false_and_persist_true(temp_workspace):
    """Test Bug 9 (Point 3): cache: false and persist: true outputs are not purged or marked as corrupted."""
    ws, _ = temp_workspace

    metrics_file = os.path.join(ws, "metrics.json")
    with open(metrics_file, "w") as f:
        f.write('{"loss": 0.05}')

    dvc_yaml = """schema: '2.0'
stages:
  eval:
    cmd: python eval.py
    outs:
      - metrics.json:
          cache: false
          persist: true
"""
    with open(os.path.join(ws, "dvc.yaml"), "w", encoding="utf-8") as f:
        f.write(dvc_yaml)

    res = sanitize_workspace(ws, dvc_checkout=False, strict_lock_check=True)
    assert os.path.isfile(metrics_file)
    assert len(res["mismatches"]) == 0


def test_sanitize_cli_entrypoint(temp_workspace):
    """Test Bug 9: CLI entrypoint executes cleanly and prints verification report."""
    ws, _ = temp_workspace
    cmd = [sys.executable, "-m", "src.runner.workspace_sanitizer", ws, "--no-checkout"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "Workspace" in proc.stdout
    assert "is clean and verified" in proc.stdout


def test_empty_cache_no_exception_listed_as_missing():
    """Test Bug 9 (Critique): empty cache on fresh worker does not raise exception, missing outputs are listed."""
    td = tempfile.mkdtemp(prefix="test_fresh_worker_")
    try:
        subprocess.run(["git", "init"], cwd=td, check=True, capture_output=True)
        subprocess.run(["dvc", "init", "--no-scm"], cwd=td, check=True, capture_output=True)

        # dvc.yaml and dvc.lock declare outputs that do not exist in local cache or on disk
        with open(os.path.join(td, "dvc.yaml"), "w", encoding="utf-8") as f:
            f.write("stages:\n  train:\n    cmd: python train.py\n    outs:\n    - model.bin\n    - eval_results.csv\n")

        with open(os.path.join(td, "dvc.lock"), "w", encoding="utf-8") as f:
            f.write(
                "schema: '2.0'\nstages:\n  train:\n    cmd: python train.py\n    outs:\n"
                "      - path: model.bin\n        md5: '11111111111111111111111111111111'\n"
                "      - path: eval_results.csv\n        md5: '22222222222222222222222222222222'\n"
            )

        # Execute full sanitize with checkout and lock verification
        res = sanitize_workspace(td, dvc_checkout=True, strict_lock_check=True)

        # Must not raise, and missing outputs must be tracked
        assert len(res["mismatches"]) == 0
        assert "model.bin" in res["missing_outputs"]
        assert "eval_results.csv" in res["missing_outputs"]
        assert res["checked_outputs"] == 0

        # Contrast: if a file IS present on disk with mismatched hash (e.g. without checkout restoring/purging), it MUST fail
        with open(os.path.join(td, "model.bin"), "wb") as f:
            f.write(b"corrupted or wrong epoch model\n")

        with pytest.raises(WorkspaceSanitizerError) as excinfo:
            sanitize_workspace(td, dvc_checkout=False, strict_lock_check=True)

        assert "Écart d'intégrité détecté" in str(excinfo.value)
        assert "model.bin" in str(excinfo.value)
    finally:
        shutil.rmtree(td, onerror=_remove_readonly, ignore_errors=True)

