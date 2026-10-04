import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest
from ruamel.yaml import YAML

from src.runner.dvc_git_helper import (
    sync_before_node,
    push_with_retries,
    get_allowed_sync_paths,
    is_path_allowed,
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


@pytest.fixture
def git_environment(tmp_path):
    """Setup a bare remote and an initial commit C0 with code and dvc configuration."""
    bare_dir = tmp_path / "origin.git"
    env = _get_git_env()

    # Create bare repo
    subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True, env=env)

    # Seed clone
    seed_clone = tmp_path / "seed_clone"
    subprocess.run(["git", "clone", str(bare_dir), str(seed_clone)], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(seed_clone), "config", "user.name", "Seed Author"], check=True, env=env)
    subprocess.run(["git", "-C", str(seed_clone), "config", "user.email", "seed@cluster-ci.io"], check=True, env=env)

    # C0 files
    (seed_clone / "train.py").write_text("print('version 1 code')\n")
    (seed_clone / "params.yaml").write_text("learning_rate: 0.01\n")

    dvc_yaml_data = {
        "stages": {
            "train": {
                "cmd": "python train.py",
                "outs": [{"metrics/eval.json": {"cache": False}}],
            },
            "evaluate": {
                "cmd": "python eval.py",
                "outs": [{"plots/loss.png": {"cache": False}}],
            },
        }
    }
    _write_yaml(seed_clone / "dvc.yaml", dvc_yaml_data)

    dvc_lock_data = {
        "schema": "2.0",
        "stages": {
            "init_stage": {
                "cmd": "echo init",
                "outs": [{"path": "init.txt", "md5": "00000000000000000000000000000000"}],
            }
        },
    }
    _write_yaml(seed_clone / "dvc.lock", dvc_lock_data)

    subprocess.run(["git", "-C", str(seed_clone), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(seed_clone), "commit", "-m", "Initial commit C0"], check=True, env=env)
    subprocess.run(["git", "-C", str(seed_clone), "branch", "-M", "main"], check=True, env=env)
    subprocess.run(["git", "-C", str(seed_clone), "push", "-u", "origin", "main"], check=True, env=env)

    c0_sha = subprocess.check_output(["git", "-C", str(seed_clone), "rev-parse", "HEAD"], text=True, env=env).strip()

    return {
        "bare_dir": str(bare_dir),
        "c0_sha": c0_sha,
        "env": env,
    }


class TestTargetedGitSync:
    """Test suite for targeted git synchronization and bot push without moving code HEAD."""

    def test_targeted_sync_preserves_code_head_and_brings_only_outputs(self, git_environment, tmp_path, capsys):
        """Commit bot + commit humain de code -> seul le dvc.lock/les sorties arrivent, HEAD du code inchangé.
        
        Log 'N commits humains ignorés jusqu'à la relance' visible.
        """
        bare_dir = git_environment["bare_dir"]
        c0_sha = git_environment["c0_sha"]
        env = git_environment["env"]

        # Human clone pushes H1 (modifies train.py and params.yaml)
        c_human = tmp_path / "c_human"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_human)], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(c_human), "config", "user.name", "Alice Dev"], check=True, env=env)
        subprocess.run(["git", "-C", str(c_human), "config", "user.email", "alice@company.com"], check=True, env=env)

        (c_human / "train.py").write_text("print('v2 human code modification')\n")
        (c_human / "params.yaml").write_text("learning_rate: 0.999\n")
        subprocess.run(["git", "-C", str(c_human), "commit", "-am", "feat: refactor model architecture"], check=True, env=env)
        subprocess.run(["git", "-C", str(c_human), "push", "origin", "main"], check=True, env=env)

        # Worker clone (on another machine) completes stage 'train' and pushes bot commit B1
        c_worker_1 = tmp_path / "c_worker_1"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_worker_1)], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(c_worker_1), "config", "user.name", "Worker 1"], check=True, env=env)
        subprocess.run(["git", "-C", str(c_worker_1), "config", "user.email", "w1@cluster-ci.io"], check=True, env=env)

        # Worker 1 pushes B1 with dvc.lock and metrics/eval.json
        os.makedirs(c_worker_1 / "metrics", exist_ok=True)
        (c_worker_1 / "metrics" / "eval.json").write_text('{"accuracy": 0.97}\n')
        lock_w1 = _read_yaml(c_worker_1 / "dvc.lock")
        lock_w1["stages"]["train"] = {
            "cmd": "python train.py",
            "outs": [{"path": "metrics/eval.json", "md5": "11111111111111111111111111111111"}],
        }
        _write_yaml(c_worker_1 / "dvc.lock", lock_w1)

        push_with_retries(
            current_branch="main",
            cwd=str(c_worker_1),
            files_to_commit=["dvc.lock", "metrics/eval.json"],
            start_commit=c0_sha,
        )

        # Worker 2 was submitted with commit C0
        c_worker_2 = tmp_path / "c_worker_2"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_worker_2)], check=True, capture_output=True, env=env)
        # Reset worker 2 specifically to C0 to simulate running a job from C0
        subprocess.run(["git", "-C", str(c_worker_2), "reset", "--hard", c0_sha], check=True, capture_output=True, env=env)
        (c_worker_2 / ".cluster-ci-start-commit").write_text(c0_sha + "\n")

        # Worker 2 runs sync_before_node before executing stage evaluate
        sync_before_node(current_branch="main", cwd=str(c_worker_2), start_commit=c0_sha)

        # VERIFICATIONS:
        # 1. HEAD of code repository on Worker 2 is STILL C0!
        w2_head = subprocess.check_output(["git", "-C", str(c_worker_2), "rev-parse", "HEAD"], text=True, env=env).strip()
        assert w2_head == c0_sha, f"HEAD of Worker 2 moved to {w2_head} instead of staying at {c0_sha}"

        # 2. Code files are STILL at version 1 (human modifications NOT pulled into working tree)
        assert (c_worker_2 / "train.py").read_text() == "print('version 1 code')\n"
        assert (c_worker_2 / "params.yaml").read_text() == "learning_rate: 0.01\n"

        # 3. Output files from Worker 1 ARE restored!
        assert (c_worker_2 / "metrics" / "eval.json").read_text() == '{"accuracy": 0.97}\n'
        w2_lock = _read_yaml(c_worker_2 / "dvc.lock")
        assert "train" in w2_lock["stages"]
        assert w2_lock["stages"]["train"]["outs"][0]["md5"] == "11111111111111111111111111111111"

        # 4. Check log output for human commits ignored
        captured = capsys.readouterr()
        assert "1 commits humains ignorés jusqu'à la relance" in captured.out

    def test_bot_push_on_top_of_human_commit_without_overwriting(self, git_environment, tmp_path):
        """Bot push on top of human commit without overwriting human code or reintroducing old code."""
        bare_dir = git_environment["bare_dir"]
        c0_sha = git_environment["c0_sha"]
        env = git_environment["env"]

        # Human commits H1
        c_human = tmp_path / "c_human"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_human)], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(c_human), "config", "user.name", "Bob Dev"], check=True, env=env)
        subprocess.run(["git", "-C", str(c_human), "config", "user.email", "bob@company.com"], check=True, env=env)

        (c_human / "train.py").write_text("print('version 2 Bob code')\n")
        (c_human / "new_helper.py").write_text("def helper(): pass\n")
        subprocess.run(["git", "-C", str(c_human), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(c_human), "commit", "-m", "feat: Bob adds new helper"], check=True, env=env)
        subprocess.run(["git", "-C", str(c_human), "push", "origin", "main"], check=True, env=env)
        h1_sha = subprocess.check_output(["git", "-C", str(c_human), "rev-parse", "HEAD"], text=True, env=env).strip()

        # Worker is running on commit C0
        c_worker = tmp_path / "c_worker"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_worker)], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(c_worker), "reset", "--hard", c0_sha], check=True, capture_output=True, env=env)

        # Worker generates new plot
        os.makedirs(c_worker / "plots", exist_ok=True)
        (c_worker / "plots" / "loss.png").write_text("loss_plot_binary_data")
        lock_w = _read_yaml(c_worker / "dvc.lock")
        lock_w["stages"]["evaluate"] = {
            "cmd": "python eval.py",
            "outs": [{"path": "plots/loss.png", "md5": "22222222222222222222222222222222"}],
        }
        _write_yaml(c_worker / "dvc.lock", lock_w)

        # Worker pushes plot and lock
        success = push_with_retries(
            current_branch="main",
            cwd=str(c_worker),
            files_to_commit=["dvc.lock", "plots/loss.png"],
            start_commit=c0_sha,
        )
        assert success is True

        # VERIFICATIONS:
        # 1. Local worker HEAD is STILL at C0!
        assert subprocess.check_output(["git", "-C", str(c_worker), "rev-parse", "HEAD"], text=True, env=env).strip() == c0_sha
        assert (c_worker / "train.py").read_text() == "print('version 1 code')\n"
        assert not (c_worker / "new_helper.py").exists()

        # 2. Remote branch now has a new commit whose parent is H1!
        verify_clone = tmp_path / "verify_clone"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(verify_clone)], check=True, capture_output=True, env=env)
        parent_sha = subprocess.check_output(["git", "-C", str(verify_clone), "rev-parse", "HEAD~1"], text=True, env=env).strip()
        assert parent_sha == h1_sha

        # 3. Remote commit preserves Bob's code and new_helper.py!
        assert (verify_clone / "train.py").read_text() == "print('version 2 Bob code')\n"
        assert (verify_clone / "new_helper.py").read_text() == "def helper(): pass\n"

        # 4. Remote commit contains the worker's output!
        assert (verify_clone / "plots" / "loss.png").read_text() == "loss_plot_binary_data"
        final_lock = _read_yaml(verify_clone / "dvc.lock")
        assert "evaluate" in final_lock["stages"]

    def test_two_workers_concurrency_and_retry(self, git_environment, tmp_path):
        """Two concurrent workers push different stage outputs to origin; both succeed via 3-way merge and retry."""
        bare_dir = git_environment["bare_dir"]
        c0_sha = git_environment["c0_sha"]
        env = git_environment["env"]

        # Worker A
        c_wa = tmp_path / "c_wa"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_wa)], check=True, capture_output=True, env=env)
        os.makedirs(c_wa / "metrics", exist_ok=True)
        (c_wa / "metrics" / "eval.json").write_text('{"wa": 1}')
        lock_a = _read_yaml(c_wa / "dvc.lock")
        lock_a["stages"]["train"] = {"cmd": "python train.py", "outs": [{"path": "metrics/eval.json", "md5": "aaa"}]}
        _write_yaml(c_wa / "dvc.lock", lock_a)

        # Worker B
        c_wb = tmp_path / "c_wb"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_wb)], check=True, capture_output=True, env=env)
        os.makedirs(c_wb / "plots", exist_ok=True)
        (c_wb / "plots" / "loss.png").write_text("loss plot")
        lock_b = _read_yaml(c_wb / "dvc.lock")
        lock_b["stages"]["evaluate"] = {"cmd": "python eval.py", "outs": [{"path": "plots/loss.png", "md5": "bbb"}]}
        _write_yaml(c_wb / "dvc.lock", lock_b)

        # Push Worker A
        success_a = push_with_retries(
            current_branch="main",
            cwd=str(c_wa),
            files_to_commit=["dvc.lock", "metrics/eval.json"],
            start_commit=c0_sha,
        )
        assert success_a is True

        # Push Worker B (concurrent push: will reconcile dvc.lock and retry)
        success_b = push_with_retries(
            current_branch="main",
            cwd=str(c_wb),
            files_to_commit=["dvc.lock", "plots/loss.png"],
            start_commit=c0_sha,
        )
        assert success_b is True

        # Verify origin has both outputs and merged dvc.lock
        v_clone = tmp_path / "v_clone"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(v_clone)], check=True, capture_output=True, env=env)
        assert (v_clone / "metrics" / "eval.json").read_text() == '{"wa": 1}'
        assert (v_clone / "plots" / "loss.png").read_text() == "loss plot"
        v_lock = _read_yaml(v_clone / "dvc.lock")
        assert "train" in v_lock["stages"]
        assert "evaluate" in v_lock["stages"]

    def test_dvc_lock_conflict_fails_loudly(self, git_environment, tmp_path):
        """Conflicting modifications to the same stage in dvc.lock must fail loudly without silent fallback."""
        bare_dir = git_environment["bare_dir"]
        c0_sha = git_environment["c0_sha"]
        env = git_environment["env"]

        # Worker A modifies init_stage md5 to 'aaa'
        c_wa = tmp_path / "c_wa"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_wa)], check=True, capture_output=True, env=env)
        lock_a = _read_yaml(c_wa / "dvc.lock")
        lock_a["stages"]["init_stage"]["outs"][0]["md5"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        _write_yaml(c_wa / "dvc.lock", lock_a)
        push_with_retries(current_branch="main", cwd=str(c_wa), files_to_commit=["dvc.lock"], start_commit=c0_sha)

        # Worker B modifies init_stage md5 to 'bbb' (conflict)
        c_wb = tmp_path / "c_wb"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_wb)], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(c_wb), "reset", "--hard", c0_sha], check=True, capture_output=True, env=env)
        lock_b = _read_yaml(c_wb / "dvc.lock")
        lock_b["stages"]["init_stage"]["outs"][0]["md5"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        _write_yaml(c_wb / "dvc.lock", lock_b)

        with pytest.raises(RuntimeError, match="Reconciliation failed|Unresolvable conflict"):
            push_with_retries(current_branch="main", cwd=str(c_wb), files_to_commit=["dvc.lock"], max_retries=2, start_commit=c0_sha)

    def test_edge_case_output_removed_from_dvc_yaml_by_human(self, git_environment, tmp_path):
        """Edge case: human deletes an output from dvc.yaml on origin after job submission.
        Job's allowed paths still derive from C0 start commit and synchronization remains stable.
        """
        bare_dir = git_environment["bare_dir"]
        c0_sha = git_environment["c0_sha"]
        env = git_environment["env"]

        # Human deletes plots from dvc.yaml on origin
        c_human = tmp_path / "c_human"
        subprocess.run(["git", "clone", "-b", "main", bare_dir, str(c_human)], check=True, capture_output=True, env=env)
        dvc_yaml_data = {
            "stages": {
                "train": {
                    "cmd": "python train.py",
                    "outs": [{"metrics/eval.json": {"cache": False}}],
                }
                # evaluate stage deleted by human!
            }
        }
        _write_yaml(c_human / "dvc.yaml", dvc_yaml_data)
        subprocess.run(["git", "-C", str(c_human), "commit", "-am", "refactor: remove evaluate stage"], check=True, env=env)
        subprocess.run(["git", "-C", str(c_human), "push", "origin", "main"], check=True, env=env)

        # Worker with start_commit=c0_sha still recognises plots/loss.png as an allowed path
        allowed = get_allowed_sync_paths(repo_path=str(c_human), start_commit=c0_sha)
        assert is_path_allowed("plots/loss.png", allowed) is True

    def test_edge_case_git_fetch_failure_fails_loudly(self, tmp_path):
        """Git fetch failure must raise explicit RuntimeError without silent fallback."""
        fake_repo = tmp_path / "fake_repo"
        subprocess.run(["git", "init", str(fake_repo)], check=True, capture_output=True)
        # origin does not exist
        subprocess.run(["git", "-C", str(fake_repo), "remote", "add", "origin", "http://invalid-fake-host/repo.git"], check=True)

        with pytest.raises(RuntimeError, match="sync_before_node failed on branch 'main'"):
            sync_before_node(current_branch="main", cwd=str(fake_repo))

    def test_dvc_foreach_stages_resolved_and_code_rejected(self, tmp_path):
        """DVC foreach stage expansion evaluates variables like ${datasets} and resolves concrete outputs.
        Code files, params.yaml and dvc.yaml must be rejected.
        """
        repo_dir = tmp_path / "foreach_repo"
        subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Tester"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "tester@test.io"], check=True)

        params_data = {
            "datasets": ["movielens", "amazon-books", "amazon-video-games"],
        }
        _write_yaml(repo_dir / "params.yaml", params_data)

        dvc_yaml_data = {
            "stages": {
                "step_compare_metrics_zeroshot": {
                    "foreach": "${datasets}",
                    "do": {
                        "cmd": "python scripts/compare_metrics.py dataset=${item}",
                        "deps": ["scripts/compare_metrics.py"],
                        "metrics": [
                            {"results/metrics/comparative_table_zeroshot_${item}.md": {"cache": False}},
                            {"results/metrics/comparative_metrics_summary_zeroshot_${item}.csv": {"cache": False}},
                        ],
                        "plots": [
                            {"results/plots/global_comparative_benchmark_zeroshot_${item}.png": {"cache": False}},
                        ],
                    },
                },
            },
        }
        _write_yaml(repo_dir / "dvc.yaml", dvc_yaml_data)

        subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "Initial commit with foreach"], check=True)
        c0_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()

        allowed = get_allowed_sync_paths(repo_path=str(repo_dir), start_commit=c0_sha)

        # 3 datasets * 3 outputs = 9 outputs allowed
        expected_allowed = [
            "results/plots/global_comparative_benchmark_zeroshot_amazon-video-games.png",
            "results/metrics/comparative_metrics_summary_zeroshot_amazon-video-games.csv",
            "results/metrics/comparative_table_zeroshot_amazon-video-games.md",
            "results/plots/global_comparative_benchmark_zeroshot_movielens.png",
            "results/metrics/comparative_metrics_summary_zeroshot_movielens.csv",
            "results/metrics/comparative_table_zeroshot_movielens.md",
            "results/plots/global_comparative_benchmark_zeroshot_amazon-books.png",
            "results/metrics/comparative_metrics_summary_zeroshot_amazon-books.csv",
            "results/metrics/comparative_table_zeroshot_amazon-books.md",
            "dvc.lock",
        ]
        for p in expected_allowed:
            assert is_path_allowed(p, allowed) is True, f"Expected {p} to be allowed"

        # Code and config files MUST be rejected
        rejected_paths = [
            "scripts/compare_metrics.py",
            "scripts/simulate_research.py",
            "dvc.yaml",
            "params.yaml",
        ]
        for p in rejected_paths:
            assert is_path_allowed(p, allowed) is False, f"Expected {p} to be rejected"

    def test_dvc_resolution_failure_raises_runtime_error(self, tmp_path):
        """Malformed dvc.yaml or unresolvable foreach must raise explicit RuntimeError without silent fallback."""
        repo_dir = tmp_path / "broken_repo"
        subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Tester"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "tester@test.io"], check=True)

        dvc_yaml_data = {
            "stages": {
                "broken_stage": {
                    "foreach": "${missing_var}",
                    "do": {"cmd": "echo 1", "outs": ["out_${item}.txt"]},
                },
            },
        }
        _write_yaml(repo_dir / "dvc.yaml", dvc_yaml_data)
        subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "Broken foreach"], check=True)
        c0_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()

        with pytest.raises(RuntimeError, match="Failed to resolve DVC stage outputs"):
            get_allowed_sync_paths(repo_path=str(repo_dir), start_commit=c0_sha)

