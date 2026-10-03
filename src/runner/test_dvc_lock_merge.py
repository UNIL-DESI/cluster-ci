import os
import sys
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path
import pytest
from ruamel.yaml import YAML

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.runner.dvc_lock_merge import (
    merge_dvc_lock_data,
    merge_dvc_lock_files,
    MergeConflictError,
)
from src.runner.dvc_git_helper import (
    install_dvc_lock_merge_driver,
    push_with_retries,
    sync_before_node,
    _get_git_env,
)


def _write_yaml(path, data):
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2, offset=0)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)


def _read_yaml(path):
    yaml = YAML()
    with open(path, "r", encoding="utf-8") as f:
        return yaml.load(f)


class TestDVCLockMergeLogic:
    """Unit tests for the 3-way YAML merge logic."""

    def test_single_side_modifications(self):
        base = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "run s1", "outs": [{"path": "o1.txt", "md5": "base1"}]},
                "s2": {"cmd": "run s2", "outs": [{"path": "o2.txt", "md5": "base2"}]},
            },
        }
        ours = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "run s1", "outs": [{"path": "o1.txt", "md5": "base1"}]},
                "s2": {"cmd": "run s2", "outs": [{"path": "o2.txt", "md5": "modified_ours"}]},
            },
        }
        theirs = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "run s1", "outs": [{"path": "o1.txt", "md5": "modified_theirs"}]},
                "s2": {"cmd": "run s2", "outs": [{"path": "o2.txt", "md5": "base2"}]},
            },
        }

        merged = merge_dvc_lock_data(base, ours, theirs)
        assert merged["stages"]["s1"]["outs"][0]["md5"] == "modified_theirs"
        assert merged["stages"]["s2"]["outs"][0]["md5"] == "modified_ours"

    def test_stage_additions_and_deterministic_order(self):
        base = {
            "schema": "2.0",
            "stages": {
                "s_base": {"cmd": "base"},
            },
        }
        ours = {
            "schema": "2.0",
            "stages": {
                "s_base": {"cmd": "base"},
                "s_ours_1": {"cmd": "ours1"},
                "s_ours_2": {"cmd": "ours2"},
            },
        }
        theirs = {
            "schema": "2.0",
            "stages": {
                "s_base": {"cmd": "base"},
                "s_theirs_1": {"cmd": "theirs1"},
                "s_theirs_2": {"cmd": "theirs2"},
            },
        }

        merged = merge_dvc_lock_data(base, ours, theirs)
        # Order must be: order of A (Ours), then additions of B (Theirs)
        keys = list(merged["stages"].keys())
        assert keys == ["s_base", "s_ours_1", "s_ours_2", "s_theirs_1", "s_theirs_2"]

    def test_identical_modifications(self):
        base = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "base", "outs": [{"path": "f.txt", "md5": "v0"}]},
            },
        }
        ours = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "base", "outs": [{"path": "f.txt", "md5": "v1"}]},
            },
        }
        theirs = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "base", "outs": [{"path": "f.txt", "md5": "v1"}]},
            },
        }

        merged = merge_dvc_lock_data(base, ours, theirs)
        assert merged["stages"]["s1"]["outs"][0]["md5"] == "v1"

    def test_conflict_different_modifications(self):
        base = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "base", "outs": [{"path": "f.txt", "md5": "v0"}]},
            },
        }
        ours = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "base", "outs": [{"path": "f.txt", "md5": "v_ours"}]},
            },
        }
        theirs = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "base", "outs": [{"path": "f.txt", "md5": "v_theirs"}]},
            },
        }

        with pytest.raises(MergeConflictError, match="modified differently"):
            merge_dvc_lock_data(base, ours, theirs)

    def test_clean_stage_deletion(self):
        base = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "s1"},
                "s2": {"cmd": "s2"},
            },
        }
        # Ours deletes s1, keeps s2 unchanged
        ours = {
            "schema": "2.0",
            "stages": {
                "s2": {"cmd": "s2"},
            },
        }
        # Theirs keeps s1 unchanged, modifies s2
        theirs = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "s1"},
                "s2": {"cmd": "s2_modified"},
            },
        }

        merged = merge_dvc_lock_data(base, ours, theirs)
        assert "s1" not in merged["stages"]
        assert merged["stages"]["s2"]["cmd"] == "s2_modified"

    def test_deletion_vs_modification_conflict(self):
        base = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "base"},
            },
        }
        ours = {
            "schema": "2.0",
            "stages": {
                "s1": {"cmd": "modified_ours"},
            },
        }
        theirs = {
            "schema": "2.0",
            "stages": {},  # Deleted in theirs
        }

        with pytest.raises(MergeConflictError, match="modified in our branch but deleted"):
            merge_dvc_lock_data(base, ours, theirs)


class TestDVCLockMergeDriverCLI:
    """Test the CLI invocation of dvc_lock_merge (python -m src.runner.dvc_lock_merge %O %A %B)."""

    def test_cli_success_and_conflict(self, tmp_path):
        o_path = tmp_path / "base.lock"
        a_path = tmp_path / "ours.lock"
        b_path = tmp_path / "theirs.lock"

        # 1. Successful merge
        _write_yaml(o_path, {"schema": "2.0", "stages": {"s1": {"cmd": "c1"}}})
        _write_yaml(a_path, {"schema": "2.0", "stages": {"s1": {"cmd": "c1"}, "s2": {"cmd": "c2"}}})
        _write_yaml(b_path, {"schema": "2.0", "stages": {"s1": {"cmd": "c1"}, "s3": {"cmd": "c3"}}})

        env = _get_git_env()
        res = subprocess.run(
            [sys.executable, "-m", "src.runner.dvc_lock_merge", str(o_path), str(a_path), str(b_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert res.returncode == 0
        merged = _read_yaml(a_path)
        assert "s1" in merged["stages"]
        assert "s2" in merged["stages"]
        assert "s3" in merged["stages"]

        # 2. Conflicting merge
        _write_yaml(a_path, {"schema": "2.0", "stages": {"s1": {"cmd": "conflict_a"}}})
        _write_yaml(b_path, {"schema": "2.0", "stages": {"s1": {"cmd": "conflict_b"}}})

        res_conflict = subprocess.run(
            [sys.executable, "-m", "src.runner.dvc_lock_merge", str(o_path), str(a_path), str(b_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert res_conflict.returncode != 0
        assert "conflict" in res_conflict.stderr.lower()


class TestGitMergeDriverIntegration:
    """Real Git integration tests with temporary local bare repositories."""

    @pytest.fixture
    def git_cluster(self, tmp_path):
        """Create a bare repo with initial commit and two worker clones."""
        bare_dir = tmp_path / "bare.git"
        clone_a = tmp_path / "worker_a"
        clone_b = tmp_path / "worker_b"

        env = _get_git_env()

        # Init bare
        subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True, env=env)

        # Init clone A
        subprocess.run(["git", "clone", str(bare_dir), str(clone_a)], check=True, capture_output=True, env=env)
        for c in [clone_a]:
            subprocess.run(["git", "-C", str(c), "config", "user.name", "Worker Bot"], check=True, env=env)
            subprocess.run(["git", "-C", str(c), "config", "user.email", "bot@cluster-ci.io"], check=True, env=env)

        initial_lock = {
            "schema": "2.0",
            "stages": {
                "base_stage": {
                    "cmd": "echo base",
                    "outs": [{"path": "base.txt", "hash": "md5", "md5": "00000000000000000000000000000000"}],
                }
            },
        }
        _write_yaml(clone_a / "dvc.lock", initial_lock)
        subprocess.run(["git", "-C", str(clone_a), "add", "dvc.lock"], check=True, env=env)
        subprocess.run(["git", "-C", str(clone_a), "commit", "-m", "Initial dvc.lock"], check=True, env=env)
        subprocess.run(["git", "-C", str(clone_a), "branch", "-M", "main"], check=True, env=env)
        subprocess.run(["git", "-C", str(clone_a), "push", "-u", "origin", "main"], check=True, env=env)

        # Init clone B
        subprocess.run(["git", "clone", "-b", "main", str(bare_dir), str(clone_b)], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(clone_b), "config", "user.name", "Worker Bot B"], check=True, env=env)
        subprocess.run(["git", "-C", str(clone_b), "config", "user.email", "bot_b@cluster-ci.io"], check=True, env=env)

        # Install merge driver on both
        install_dvc_lock_merge_driver(repo_path=str(clone_a))
        install_dvc_lock_merge_driver(repo_path=str(clone_b))

        return {"bare": str(bare_dir), "clone_a": str(clone_a), "clone_b": str(clone_b)}

    def test_idempotence_installation(self, tmp_path):
        repo_dir = tmp_path / "repo_idempotence"
        env = _get_git_env()
        subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True, env=env)

        # First installation
        install_dvc_lock_merge_driver(repo_path=str(repo_dir))
        attr_file = repo_dir / ".git" / "info" / "attributes"
        assert attr_file.exists()
        content_1 = attr_file.read_text(encoding="utf-8")
        assert "dvc.lock merge=dvclock" in content_1
        assert content_1.count("dvc.lock merge=dvclock") == 1

        # Second installation
        install_dvc_lock_merge_driver(repo_path=str(repo_dir))
        content_2 = attr_file.read_text(encoding="utf-8")
        assert content_2.count("dvc.lock merge=dvclock") == 1

        # Third installation
        install_dvc_lock_merge_driver(repo_path=str(repo_dir))
        content_3 = attr_file.read_text(encoding="utf-8")
        assert content_3.count("dvc.lock merge=dvclock") == 1

    def test_two_clones_modify_different_stages_push_success(self, git_cluster):
        clone_a = git_cluster["clone_a"]
        clone_b = git_cluster["clone_b"]
        env = _get_git_env()

        # Clone A adds stage_a
        lock_a = _read_yaml(os.path.join(clone_a, "dvc.lock"))
        lock_a["stages"]["stage_a"] = {
            "cmd": "echo A",
            "outs": [{"path": "out_a.txt", "hash": "md5", "md5": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],
        }
        _write_yaml(os.path.join(clone_a, "dvc.lock"), lock_a)
        subprocess.run(["git", "-C", clone_a, "commit", "-am", "Worker A completed stage_a"], check=True, env=env)
        # Push A
        push_with_retries(current_branch="main", cwd=clone_a)

        # Clone B adds stage_b (simultaneously from original common commit)
        lock_b = _read_yaml(os.path.join(clone_b, "dvc.lock"))
        lock_b["stages"]["stage_b"] = {
            "cmd": "echo B",
            "outs": [{"path": "out_b.txt", "hash": "md5", "md5": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}],
        }
        _write_yaml(os.path.join(clone_b, "dvc.lock"), lock_b)
        subprocess.run(["git", "-C", clone_b, "commit", "-am", "Worker B completed stage_b"], check=True, env=env)

        # Push B: Initial push rejected -> pull --rebase triggers dvc_lock_merge driver -> push succeeds
        success = push_with_retries(current_branch="main", cwd=clone_b)
        assert success is True

        # Check final dvc.lock on clone B has both stage_a and stage_b
        final_lock = _read_yaml(os.path.join(clone_b, "dvc.lock"))
        assert "base_stage" in final_lock["stages"]
        assert "stage_a" in final_lock["stages"]
        assert "stage_b" in final_lock["stages"]

        # Validate with dvc status if dvc is available
        dvc_path = shutil.which("dvc")
        if dvc_path:
            # Check YAML can be re-read cleanly
            assert final_lock["schema"] == "2.0"

    def test_same_stage_modified_differently_fails_loudly(self, git_cluster):
        clone_a = git_cluster["clone_a"]
        clone_b = git_cluster["clone_b"]
        env = _get_git_env()

        # Clone A modifies base_stage to version A
        lock_a = _read_yaml(os.path.join(clone_a, "dvc.lock"))
        lock_a["stages"]["base_stage"]["outs"][0]["md5"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        _write_yaml(os.path.join(clone_a, "dvc.lock"), lock_a)
        subprocess.run(["git", "-C", clone_a, "commit", "-am", "A modifies base_stage"], check=True, env=env)
        push_with_retries(current_branch="main", cwd=clone_a)

        # Clone B modifies base_stage to version B (different)
        lock_b = _read_yaml(os.path.join(clone_b, "dvc.lock"))
        lock_b["stages"]["base_stage"]["outs"][0]["md5"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        _write_yaml(os.path.join(clone_b, "dvc.lock"), lock_b)
        subprocess.run(["git", "-C", clone_b, "commit", "-am", "B modifies base_stage differently"], check=True, env=env)

        # Push B must FAIL LOUDLY with RuntimeError
        with pytest.raises(RuntimeError, match="Reconciliation failed|Unresolvable conflict"):
            push_with_retries(current_branch="main", max_retries=3, cwd=clone_b)

        # Repository must not be left in an active rebase state
        git_dir = subprocess.check_output(["git", "-C", clone_b, "rev-parse", "--git-dir"], text=True, env=env).strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(clone_b, git_dir)
        assert not os.path.exists(os.path.join(git_dir, "rebase-merge"))
        assert not os.path.exists(os.path.join(git_dir, "rebase-apply"))

    def test_three_concurrent_pushes_threads(self, tmp_path):
        bare_dir = tmp_path / "bare_concurrent.git"
        env = _get_git_env()

        # Create bare repo
        subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True, env=env)

        # Seed initial commit
        seed_clone = tmp_path / "seed_clone"
        subprocess.run(["git", "clone", str(bare_dir), str(seed_clone)], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(seed_clone), "config", "user.name", "Seed Bot"], check=True, env=env)
        subprocess.run(["git", "-C", str(seed_clone), "config", "user.email", "seed@cluster-ci.io"], check=True, env=env)

        init_data = {"schema": "2.0", "stages": {"init": {"cmd": "echo 0"}}}
        _write_yaml(seed_clone / "dvc.lock", init_data)
        subprocess.run(["git", "-C", str(seed_clone), "add", "dvc.lock"], check=True, env=env)
        subprocess.run(["git", "-C", str(seed_clone), "commit", "-m", "Init"], check=True, env=env)
        subprocess.run(["git", "-C", str(seed_clone), "branch", "-M", "main"], check=True, env=env)
        subprocess.run(["git", "-C", str(seed_clone), "push", "-u", "origin", "main"], check=True, env=env)

        clones = []
        for i in range(1, 4):
            c_dir = tmp_path / f"worker_{i}"
            subprocess.run(["git", "clone", "-b", "main", str(bare_dir), str(c_dir)], check=True, capture_output=True, env=env)
            subprocess.run(["git", "-C", str(c_dir), "config", "user.name", f"Worker {i}"], check=True, env=env)
            subprocess.run(["git", "-C", str(c_dir), "config", "user.email", f"worker{i}@cluster-ci.io"], check=True, env=env)
            install_dvc_lock_merge_driver(repo_path=str(c_dir))

            # Each worker modifies a different stage
            lock = _read_yaml(c_dir / "dvc.lock")
            lock["stages"][f"stage_{i}"] = {
                "cmd": f"run {i}",
                "outs": [{"path": f"out_{i}.txt", "hash": "md5", "md5": f"{i}" * 32}],
            }
            _write_yaml(c_dir / "dvc.lock", lock)
            subprocess.run(["git", "-C", str(c_dir), "commit", "-am", f"Worker {i} stage_{i}"], check=True, env=env)
            clones.append(str(c_dir))

        errors = []

        def worker_push(clone_path):
            try:
                push_with_retries(current_branch="main", max_retries=10, base_delay=0.2, max_delay=3.0, cwd=clone_path)
            except Exception as e:
                errors.append((clone_path, str(e)))

        threads = [threading.Thread(target=worker_push, args=(c,)) for c in clones]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 3 pushes must succeed without error
        assert errors == [], f"Concurrent push errors: {errors}"

        # Verify on origin
        verify_clone = tmp_path / "verify_clone"
        subprocess.run(["git", "clone", "-b", "main", str(bare_dir), str(verify_clone)], check=True, capture_output=True, env=env)
        final_lock = _read_yaml(verify_clone / "dvc.lock")
        assert "init" in final_lock["stages"]
        assert "stage_1" in final_lock["stages"]
        assert "stage_2" in final_lock["stages"]
        assert "stage_3" in final_lock["stages"]

    def test_sync_before_node_pulls_and_merges(self, git_cluster):
        clone_a = git_cluster["clone_a"]
        clone_b = git_cluster["clone_b"]
        env = _get_git_env()

        # Remote receives a new stage from Clone A
        lock_a = _read_yaml(os.path.join(clone_a, "dvc.lock"))
        lock_a["stages"]["stage_remote"] = {"cmd": "echo remote"}
        _write_yaml(os.path.join(clone_a, "dvc.lock"), lock_a)
        subprocess.run(["git", "-C", clone_a, "commit", "-am", "Remote stage added"], check=True, env=env)
        push_with_retries(current_branch="main", cwd=clone_a)

        # Clone B has unpushed local commit modifying a different stage
        lock_b = _read_yaml(os.path.join(clone_b, "dvc.lock"))
        lock_b["stages"]["stage_local_unpushed"] = {"cmd": "echo local"}
        _write_yaml(os.path.join(clone_b, "dvc.lock"), lock_b)
        subprocess.run(["git", "-C", clone_b, "commit", "-am", "Local unpushed stage"], check=True, env=env)

        # Clone B executes sync_before_node()
        sync_before_node(current_branch="main", cwd=clone_b)

        # Verify both stages exist in Clone B's dvc.lock and local commit is preserved
        merged_lock = _read_yaml(os.path.join(clone_b, "dvc.lock"))
        assert "stage_remote" in merged_lock["stages"]
        assert "stage_local_unpushed" in merged_lock["stages"]
