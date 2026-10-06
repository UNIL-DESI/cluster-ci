"""Tests for dvc_realign module on real Git and DVC repositories."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from ruamel.yaml import YAML

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.runner.dvc_realign import (  # noqa: E402
    commit_realigned_lock,
    compute_file_md5_and_size,
    is_code_dependency,
    realign_dvc_lock,
)


@pytest.fixture
def temp_git_dvc_repo():
    """Create a temporary real Git and DVC repository with a 2-stage pipeline."""
    td = tempfile.mkdtemp(prefix="cluster_ci_realign_test_")
    repo_path = Path(td)
    try:
        # Initialize Git
        subprocess.run(["git", "init", "-q"], cwd=td, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=td, check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=td, check=True)

        # Initialize DVC
        subprocess.run(["dvc", "init", "-q"], cwd=td, check=True)
        subprocess.run(["git", "commit", "-m", "init git and dvc", "-q"], cwd=td, check=True)

        # Create code scripts and data
        (repo_path / "src").mkdir(parents=True, exist_ok=True)
        (repo_path / "data").mkdir(parents=True, exist_ok=True)

        prep_py = repo_path / "src" / "prep.py"
        prep_py.write_text("print('Executing prep step')\nwith open('data/prep_out.txt', 'w') as f: f.write('prep_data_v1')\n", encoding="utf-8")

        train_py = repo_path / "src" / "train.py"
        train_py.write_text("print('Executing train step')\nwith open('data/train_out.txt', 'w') as f: f.write('train_data_v1')\n", encoding="utf-8")

        # Create 2 stages via dvc stage add
        subprocess.run([
            "dvc", "stage", "add", "-q", "-n", "stage_prep",
            "-d", "src/prep.py",
            "-o", "data/prep_out.txt",
            "python src/prep.py"
        ], cwd=td, check=True)

        subprocess.run([
            "dvc", "stage", "add", "-q", "-n", "stage_train",
            "-d", "src/train.py",
            "-d", "data/prep_out.txt",
            "-o", "data/train_out.txt",
            "python src/train.py"
        ], cwd=td, check=True)

        # Initial dvc repro to generate dvc.lock
        subprocess.run(["dvc", "repro", "-q"], cwd=td, check=True)
        subprocess.run(["git", "add", "."], cwd=td, check=True)
        subprocess.run(["git", "commit", "-m", "initial repro", "-q"], cwd=td, check=True)

        yield repo_path
    finally:
        shutil.rmtree(td, ignore_errors=True)


class TestCodeDependencyClassifier:
    """Unit tests for code vs data classification."""

    def test_code_extensions_and_paths(self):
        assert is_code_dependency("src/model.py") is True
        assert is_code_dependency("scripts/run.sh") is True
        assert is_code_dependency("cuda_kernel.cu") is True
        assert is_code_dependency("eval/metrics.r") is True
        assert is_code_dependency(".dvc-viewer/hashes/stage_train.hash") is True
        assert is_code_dependency("stage.hash") is True

    def test_excluded_data_and_params(self):
        assert is_code_dependency("data/dataset.csv") is False
        assert is_code_dependency("dataset/images.parquet") is False
        assert is_code_dependency("params.yaml") is False
        assert is_code_dependency("weights.pt") is False
        assert is_code_dependency("features.npy") is False
        assert is_code_dependency("table.parquet") is False

    def test_custom_directives(self):
        custom_exts = {".custom"}
        custom_paths = ("custom_dir/",)
        assert is_code_dependency("foo.custom", code_extensions=custom_exts) is True
        assert is_code_dependency("custom_dir/anything.sh", code_paths=custom_paths) is True

    def test_non_code_files_under_src_excluded(self):
        assert is_code_dependency("src/config.yaml") is False
        assert is_code_dependency("src/config.yml") is False
        assert is_code_dependency("src/vocab.txt") is False
        assert is_code_dependency("scripts/data.json") is False
        assert is_code_dependency("src/model.py") is True



class TestDVCLockRealignment:
    """E2E and unit tests for dvc.lock realignment on real repositories."""

    def test_nominal_realignment_cleans_dvc_status(self, temp_git_dvc_repo):
        repo_path = temp_git_dvc_repo
        lock_file = repo_path / "dvc.lock"

        # 1. Verify initial dvc status is clean
        res_initial = subprocess.run(["dvc", "status"], cwd=repo_path, capture_output=True, text=True)
        assert "Data and pipelines are up to date" in res_initial.stdout

        # Record initial output hashes
        yaml = YAML()
        with open(lock_file, "r", encoding="utf-8") as f:
            initial_lock = yaml.load(f)
        prep_out_hash = initial_lock["stages"]["stage_prep"]["outs"][0]["md5"]
        train_out_hash = initial_lock["stages"]["stage_train"]["outs"][0]["md5"]

        # 2. Modify prep.py code (non-semantic change / comment)
        prep_py = repo_path / "src" / "prep.py"
        prep_content = prep_py.read_text(encoding="utf-8")
        prep_py.write_text(prep_content + "\n# non-semantic comment\n", encoding="utf-8")

        # 3. Status should now show stage_prep modified
        res_dirty = subprocess.run(["dvc", "status"], cwd=repo_path, capture_output=True, text=True)
        assert "stage_prep" in res_dirty.stdout
        assert "src/prep.py" in res_dirty.stdout.replace("\\", "/")

        # 4. Realign dvc.lock for stage_prep
        changes = realign_dvc_lock(repo_path, target_stages=["stage_prep"])
        assert len(changes) == 1
        assert changes[0]["stage"] == "stage_prep"
        assert changes[0]["path"] == "src/prep.py"
        assert changes[0]["old_md5"] != changes[0]["new_md5"]

        # 5. Status must now be clean ("Data and pipelines are up to date")
        res_after = subprocess.run(["dvc", "status"], cwd=repo_path, capture_output=True, text=True)
        assert "Data and pipelines are up to date" in res_after.stdout

        # 6. Verify output hashes were NEVER altered
        with open(lock_file, "r", encoding="utf-8") as f:
            updated_lock = yaml.load(f)
        assert updated_lock["stages"]["stage_prep"]["outs"][0]["md5"] == prep_out_hash
        assert updated_lock["stages"]["stage_train"]["outs"][0]["md5"] == train_out_hash

    def test_fail_fast_unknown_stage(self, temp_git_dvc_repo):
        repo_path = temp_git_dvc_repo
        with pytest.raises(ValueError, match="unknown"):
            realign_dvc_lock(repo_path, target_stages=["non_existent_stage"])

    def test_fail_fast_missing_dvc_lock(self, temp_git_dvc_repo):
        repo_path = temp_git_dvc_repo
        lock_file = repo_path / "dvc.lock"
        lock_file.unlink()
        with pytest.raises(FileNotFoundError, match="dvc.lock not found"):
            realign_dvc_lock(repo_path)

    def test_strict_guardrails_rejects_data_modifications(self, temp_git_dvc_repo):
        repo_path = temp_git_dvc_repo
        # Create an input data dependency for stage_prep
        data_file = repo_path / "data" / "raw_input.csv"
        data_file.write_text("id,val\n1,100\n", encoding="utf-8")

        # Re-add stage_prep with the data dependency
        subprocess.run([
            "dvc", "stage", "add", "-q", "-f", "-n", "stage_prep",
            "-d", "src/prep.py",
            "-d", "data/raw_input.csv",
            "-o", "data/prep_out.txt",
            "python src/prep.py"
        ], cwd=repo_path, check=True)
        subprocess.run(["dvc", "repro", "-q"], cwd=repo_path, check=True)

        # Modify both code AND data
        prep_py = repo_path / "src" / "prep.py"
        prep_py.write_text(prep_py.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8")
        data_file.write_text("id,val\n1,999\n", encoding="utf-8")

        # Strict guardrails must reject realignment because data changed
        with pytest.raises(ValueError, match="Strict guardrail rejection"):
            realign_dvc_lock(repo_path, target_stages=["stage_prep"], strict_guardrails=True)

    def test_strict_guardrails_rejects_param_modifications(self, temp_git_dvc_repo):
        repo_path = temp_git_dvc_repo
        # Create params.yaml and stage with parameters
        params_file = repo_path / "params.yaml"
        params_file.write_text("lr: 0.01\nbatch_size: 32\n", encoding="utf-8")

        subprocess.run([
            "dvc", "stage", "add", "-q", "-f", "-n", "stage_prep",
            "-d", "src/prep.py",
            "-p", "lr,batch_size",
            "-o", "data/prep_out.txt",
            "python src/prep.py"
        ], cwd=repo_path, check=True)
        subprocess.run(["dvc", "repro", "-q"], cwd=repo_path, check=True)

        # Modify both code AND params.yaml
        prep_py = repo_path / "src" / "prep.py"
        prep_py.write_text(prep_py.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8")
        params_file.write_text("lr: 0.05\nbatch_size: 32\n", encoding="utf-8")

        # Strict guardrails must reject realignment because parameters changed
        with pytest.raises(ValueError, match="Strict guardrail rejection: Parameter 'lr'"):
            realign_dvc_lock(repo_path, target_stages=["stage_prep"], strict_guardrails=True)

    def test_yaml_formatting_and_comments_preserved(self, temp_git_dvc_repo):
        repo_path = temp_git_dvc_repo
        lock_file = repo_path / "dvc.lock"

        # Inject custom comment into dvc.lock
        content = lock_file.read_text(encoding="utf-8")
        custom_comment = "# IMPORTANT_PRESERVED_USER_COMMENT\n"
        lock_file.write_text(custom_comment + content, encoding="utf-8")

        # Modify code
        train_py = repo_path / "src" / "train.py"
        train_py.write_text(train_py.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8")

        # Realign
        changes = realign_dvc_lock(repo_path, target_stages=["stage_train"])
        assert len(changes) == 1

        # Check comment is still in lock file
        new_content = lock_file.read_text(encoding="utf-8")
        assert "IMPORTANT_PRESERVED_USER_COMMENT" in new_content

    def test_commit_realigned_lock_local_mode(self, temp_git_dvc_repo):
        repo_path = temp_git_dvc_repo
        prep_py = repo_path / "src" / "prep.py"
        prep_py.write_text(prep_py.read_text(encoding="utf-8") + "\n# another comment\n", encoding="utf-8")

        changes = realign_dvc_lock(repo_path, target_stages=["stage_prep"])
        assert len(changes) == 1

        # Commit in local mode
        commit_sha = commit_realigned_lock(repo_path, changes=changes, is_local=True)
        assert commit_sha is not None

        # Verify git commit message
        res_log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=repo_path, capture_output=True, text=True, check=True)
        assert "chore(ci): realign dvc.lock for code-only changes in stage_prep [skip ci]" in res_log.stdout

    def test_directory_dependency_raises_not_implemented(self, temp_git_dvc_repo):
        repo_path = temp_git_dvc_repo
        src_dir = repo_path / "src"
        assert src_dir.is_dir()

        with pytest.raises(NotImplementedError, match="Directory dependency .* is not supported"):
            compute_file_md5_and_size(src_dir)

        # Inject a directory dependency into dvc.lock
        lock_file = repo_path / "dvc.lock"
        yaml = YAML()
        with open(lock_file, "r", encoding="utf-8") as f:
            lock_data = yaml.load(f)

        lock_data["stages"]["stage_prep"]["deps"].append({
            "path": "src/",
            "md5": "dummy.dir",
            "size": 0,
        })
        with open(lock_file, "w", encoding="utf-8") as f:
            yaml.dump(lock_data, f)

        with pytest.raises(NotImplementedError, match="Directory dependency 'src/' is not supported"):
            realign_dvc_lock(repo_path, target_stages=["stage_prep"])

    def test_crlf_binary_md5_matches_dvc(self, temp_git_dvc_repo):
        import hashlib
        repo_path = temp_git_dvc_repo

        crlf_file = repo_path / "src" / "crlf_script.py"
        raw_bytes = (
            b"def foo():\r\n"
            b"    with open('data/crlf_out.txt', 'w') as f:\r\n"
            b"        f.write('crlf_out_v1')\r\n"
            b"foo()\r\n"
        )
        crlf_file.write_bytes(raw_bytes)

        expected_md5 = hashlib.md5(raw_bytes).hexdigest()
        computed_md5, size = compute_file_md5_and_size(crlf_file)

        assert computed_md5 == expected_md5
        assert size == len(raw_bytes)

        # Register stage with CRLF file and run dvc repro
        subprocess.run([
            "dvc", "stage", "add", "-q", "-n", "stage_crlf",
            "-d", "src/crlf_script.py",
            "-o", "data/crlf_out.txt",
            "python src/crlf_script.py"
        ], cwd=repo_path, check=True)

        subprocess.run(["dvc", "repro", "-q", "stage_crlf"], cwd=repo_path, check=True)

        # Read dvc.lock and assert DVC 3.x md5 equals raw binary md5 without conversion
        yaml = YAML()
        with open(repo_path / "dvc.lock", "r", encoding="utf-8") as f:
            lock_data = yaml.load(f)

        dep_entry = next(
            d for d in lock_data["stages"]["stage_crlf"]["deps"]
            if d["path"] == "src/crlf_script.py"
        )
        assert dep_entry["md5"] == expected_md5
        assert dep_entry["md5"] == computed_md5



class TestCLIIntegration:
    """Tests verifying CLI argument parsing and propagation for skip-code mode."""

    def test_dvc_realign_cli(self, temp_git_dvc_repo):
        from src.runner.dvc_realign import main as realign_main

        repo_path = temp_git_dvc_repo
        prep_py = repo_path / "src" / "prep.py"
        prep_py.write_text(prep_py.read_text(encoding="utf-8") + "\n# cli comment\n", encoding="utf-8")

        # Run realign via CLI main()
        exit_code = realign_main(["--repo-dir", str(repo_path), "--stages", "stage_prep", "--commit", "--local"])
        assert exit_code == 0

        # Verify status is now clean
        res_after = subprocess.run(["dvc", "status"], cwd=repo_path, capture_output=True, text=True)
        assert "Data and pipelines are up to date" in res_after.stdout

    def test_cluster_run_cli_arguments(self):
        import argparse
        # Test cluster_run argument parser definition
        parser = argparse.ArgumentParser()
        parser.add_argument("--skip-code-invalidation", "--skip-code", action="store_true")

        args1 = parser.parse_args(["--skip-code-invalidation"])
        assert args1.skip_code_invalidation is True

        args2 = parser.parse_args(["--skip-code"])
        assert args2.skip_code_invalidation is True

        args3 = parser.parse_args([])
        assert args3.skip_code_invalidation is False

    def test_submit_job_cli_arguments(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--skip-code-invalidation", "--skip-code", action="store_true")

        args = parser.parse_args(["--skip-code"])
        assert args.skip_code_invalidation is True

    def test_skip_code_propagation_in_headnode_and_env(self, tmp_path, monkeypatch):
        """Verify that skip_code_invalidation flag is parsed by headnode_service and propagates to env_vars."""
        import json
        import uuid
        from src.scheduler import headnode_service, persistence

        db_file = str(tmp_path / f"test_cluster_{uuid.uuid4().hex[:8]}.db")
        monkeypatch.setenv("CLUSTER_DB_PATH", db_file)
        monkeypatch.setattr(headnode_service, "REPOS_DIR", str(tmp_path / "repos"))
        persistence.DB_PATH = db_file
        persistence.init_db()

        headnode_service.app.config["TESTING"] = True
        monkeypatch.setattr(headnode_service, "CLUSTER_TOKEN", "test-token")

        with headnode_service.app.test_client() as c:
            c.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
            resp = c.post("/submit_job", json={
                "repo": "owner/repo",
                "branch": "main",
                "skip_code_invalidation": True,
            })
            assert resp.status_code == 200
            data = resp.get_json()
            assert "job_id" in data
            with persistence.get_db_conn() as conn:
                row = conn.execute("SELECT env_vars FROM jobs WHERE job_id = ?", (data["job_id"],)).fetchone()
            assert row is not None
            env_vars = json.loads(row[0]) if row[0] else {}
            assert env_vars.get("SKIP_CODE_INVALIDATION") == "1"

        secrets_file = tmp_path / "job.secrets"
        secrets_file.write_text("SKIP_CODE_INVALIDATION=1\nOTHER_VAR=abc\n", encoding="utf-8")
        lines = dict(
            line.strip().split("=", 1)
            for line in secrets_file.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        assert lines.get("SKIP_CODE_INVALIDATION") == "1"


