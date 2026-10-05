"""Local jobs must not stage or synchronize Git, even with origin configured.

All repository contents are synthetic; remotes are temporary local directories.
Docker and scheduler operations are mocked.
"""
import subprocess
from unittest.mock import Mock

import pytest

from src.runner import branch_executor as branch
from src.runner import dvc_git_helper as helper


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    monkeypatch.delenv("IS_LOCAL", raising=False)
    # Isolate Git configuration; the only remote is a local bare repository.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    repo = tmp_path / "workspace"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "Synthetic Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "dvc.yaml").write_text(
        "stages:\n  evaluate:\n    cmd: echo synthetic\n"
        "    outs:\n    - metrics.json:\n        cache: false\n"
    )
    (repo / "dvc.lock").write_text("schema: '2.0'\nstages: {}\n")
    (repo / ".gitignore").write_text("metrics.json\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Synthetic initial state")
    git(repo, "remote", "add", "origin", remote.as_uri())
    git(repo, "push", "origin", "main")
    return repo, remote


def test_local_node_preserves_files_index_and_remote(repositories, monkeypatch):
    repo, remote = repositories
    before_head = git(repo, "rev-parse", "HEAD")
    before_remote = git(remote, "rev-parse", "main")
    (repo / "dvc.lock").write_text("schema: '2.0'\nstages: {synthetic: {}}\n")
    (repo / "metrics.json").write_text('{"synthetic_score": 0.75}\n')
    before_status = git(repo, "status", "--porcelain", "--ignored")
    monkeypatch.setenv("IS_LOCAL", "1")

    # This also detects a premature checkout/pull that would discard local work.
    with monkeypatch.context() as guarded:
        run = Mock(side_effect=AssertionError("local sync invoked a subprocess"))
        guarded.setattr(helper.subprocess, "run", run)
        branch.sync_before_node(str(repo), "main")
        assert branch.commit_and_push_node(
            str(repo), "evaluate", "main", [{"path": "metrics.json", "cache": False}]
        ) is True
        assert helper.push_with_retries(current_branch="main", cwd=str(repo)) is True
        run.assert_not_called()

    assert git(repo, "rev-parse", "HEAD") == before_head
    assert git(remote, "rev-parse", "main") == before_remote
    assert git(repo, "status", "--porcelain", "--ignored") == before_status
    assert "synthetic" in (repo / "dvc.lock").read_text()
    assert (repo / "metrics.json").read_text() == '{"synthetic_score": 0.75}\n'


@pytest.mark.parametrize("mode", [None, "0"])
def test_nonlocal_node_still_synchronizes(repositories, monkeypatch, mode):
    repo, remote = repositories
    if mode is not None:
        monkeypatch.setenv("IS_LOCAL", mode)
    before_remote = git(remote, "rev-parse", "main")
    branch.sync_before_node(str(repo), "main")
    (repo / "metrics.json").write_text('{"synthetic_score": 0.75}\n')
    assert branch.commit_and_push_node(
        str(repo), "evaluate", "main", [{"path": "metrics.json", "cache": False}]
    ) is True
    assert git(remote, "rev-parse", "main") != before_remote
    assert git(remote, "show", "main:metrics.json") == '{"synthetic_score": 0.75}'


@pytest.mark.parametrize("mode", [None, "0", "1"])
def test_container_recreation_preserves_local_mode(tmp_path, monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("IS_LOCAL", raising=False)
    else:
        monkeypatch.setenv("IS_LOCAL", mode)
    docker = Mock(spec=branch.DockerRunner)
    docker.create_volume.return_value = 0
    docker.run_container.return_value = 0
    docker.exec_in_container.return_value = (0, "")
    executor = branch.BranchExecutor(
        headnode_url="http://127.0.0.1:1", job_id="synthetic-job",
        runner_id="synthetic-runner", worker_id="synthetic-worker",
        repo_dir=str(tmp_path), target_repo="synthetic/project",
        target_branch="main", docker=docker,
    )
    executor.start_container_for_image("synthetic:first")
    executor.stop_current_container()
    executor.start_container_for_image("synthetic:second")
    assert docker.run_container.call_count == 2
    for call in docker.run_container.call_args_list:
        assert call.kwargs["env"]["IS_LOCAL"] == (mode or "0")


def test_local_metrics_keep_existing_http_transport(monkeypatch):
    monkeypatch.setenv("IS_LOCAL", "1")
    http_sync = Mock(return_value=True)
    monkeypatch.setattr(helper, "_sync_metrics_http", http_sync)
    monkeypatch.setattr(helper.subprocess, "run", Mock(side_effect=AssertionError("Git called")))
    assert helper.sync_metrics() is True
    http_sync.assert_called_once_with()


def test_local_branch_completes_nodes_across_image_switch(repositories, monkeypatch):
    repo, remote = repositories
    before_head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("IS_LOCAL", "1")
    docker = Mock(spec=branch.DockerRunner)
    docker.create_volume.return_value = 0
    docker.run_container.return_value = 0
    docker.exec_in_container.return_value = (0, "")
    executor = branch.BranchExecutor(
        headnode_url="http://127.0.0.1:1", job_id="synthetic-job",
        runner_id="synthetic-runner", worker_id="synthetic-worker",
        repo_dir=str(repo), target_repo="synthetic/project",
        target_branch="main", docker=docker,
    )
    monkeypatch.setattr(executor, "_start_heartbeat", Mock())
    next_node = Mock(side_effect=[
        {"action": "run", "node": "prepare", "image": "synthetic:first"},
        {"action": "switch_image", "node": "evaluate", "image": "synthetic:second"},
        {"action": "finish"},
    ])
    monkeypatch.setattr(executor, "call_next_node", next_node)

    def execute(node, gpu_ids_str):
        (repo / "metrics.json").write_text('{"synthetic_stage": "' + node + '"}\n')
        return 0, ""

    monkeypatch.setattr(executor, "execute_node_in_container", execute)
    # Exercise the real run loop and Git helpers; no Docker daemon or network.
    assert executor.run() == 0
    assert [call.kwargs["status"] for call in next_node.call_args_list] == [None, "done", "done"]
    assert [call.kwargs["env"]["IS_LOCAL"] for call in docker.run_container.call_args_list] == ["1", "1"]
    assert git(repo, "rev-parse", "HEAD") == before_head
    assert git(remote, "rev-parse", "main") == before_head
    assert (repo / "metrics.json").read_text() == '{"synthetic_stage": "evaluate"}\n'
