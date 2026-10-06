"""
test_bug12_retry_and_attempt.py - Validation du Bug 12 :
Retries bornés par nœud (défaut 2, configurable via CLUSTER_CI_MAX_NODE_RETRIES),
interdiction formelle de retry sur HostMemoryPressureExceeded (garde GB10),
contrat CLUSTER_CI_NODE_ATTEMPT (1..N) et propagation des env_vars,
observabilité API étendue dans /job_status.
"""

from pathlib import Path
import os
import sys
import uuid
import pytest

sched_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "scheduler")
if sched_dir not in sys.path:
    sys.path.insert(0, sched_dir)

import persistence  # noqa: E402
import scheduler_loop  # noqa: E402
import headnode_service  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Initialise une base SQLite temporaire et isolée."""
    db_file = str(tmp_path / f"test_cluster_bug12_{uuid.uuid4().hex[:8]}.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", db_file)
    persistence.DB_PATH = db_file
    persistence.init_db()
    yield db_file


@pytest.fixture
def client(monkeypatch):
    """Client de test Flask pour headnode_service."""
    headnode_service.app.config["TESTING"] = True
    monkeypatch.setattr(headnode_service, "CLUSTER_TOKEN", "scheduler-test-token")
    with headnode_service.app.test_client() as c:
        c.environ_base["HTTP_AUTHORIZATION"] = "Bearer scheduler-test-token"
        yield c


def test_bug12_bounded_retry_and_attempt_increment(client, monkeypatch):
    """
    Vérifie qu'un nœud échouant 2 fois est relancé jusqu'à max_retries (2 retries = 3 tentatives),
    que attempt est incrémenté (1, 2, 3), que env_vars sont transmis, et que le 3ème succès
    permet la complétion du job.
    """
    monkeypatch.setenv("CLUSTER_CI_MAX_NODE_RETRIES", "2")

    client.post("/register_worker", json={
        "worker_id": "worker1", "hostname": "worker1", "cpus": 8, "total_ram_gb": 64.0, "available_storage_gb": 500.0, "role": "worker"
    })

    plan = {
        "nodes": [
            {"name": "flaky_node", "deps": [], "resources": {"cpus": 2, "ram_gb": 4.0}, "stale": True}
        ]
    }
    sub = client.post("/submit_job", json={
        "repo": "UNIL-DESI/cluster-ci",
        "branch": "main",
        "parallel_mode": 1,
        "plan": plan,
        "env_vars": {"FAIL_STAGE": "flaky_node", "FAIL_ATTEMPTS": "2"}
    }).get_json()
    job_id = sub["job_id"]

    scheduler_loop.schedule_iteration()

    # Tentative 1
    next1 = client.post(f"/api/jobs/{job_id}/next_node", json={"worker": "worker1", "runner_id": "r1"}).get_json()
    assert next1.get("node") == "flaky_node"
    assert next1.get("attempt") == 1
    assert next1.get("env_vars", {}).get("FAIL_STAGE") == "flaky_node"

    # Tentative 1 échoue
    next2 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker": "worker1",
        "runner_id": "r1",
        "node": "flaky_node",
        "status": "failed",
        "exit_code": 1,
        "error_message": "Simulated failure on attempt 1",
    }).get_json()

    # Le nœud doit être retenté (attempt 2)
    assert next2.get("action") in ("run", "switch_image")
    assert next2.get("node") == "flaky_node"
    assert next2.get("attempt") == 2

    # Tentative 2 échoue
    next3 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker": "worker1",
        "runner_id": "r1",
        "node": "flaky_node",
        "status": "failed",
        "exit_code": 1,
        "error_message": "Simulated failure on attempt 2",
    }).get_json()

    # Le nœud doit être retenté pour la 3ème tentative (attempt 3)
    assert next3.get("action") in ("run", "switch_image")
    assert next3.get("node") == "flaky_node"
    assert next3.get("attempt") == 3

    # Tentative 3 réussit !
    next4 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker": "worker1",
        "runner_id": "r1",
        "node": "flaky_node",
        "status": "done",
        "exit_code": 0,
        "duration_s": 4.2,
    }).get_json()
    assert next4.get("action") == "finish"

    # Vérification Observabilité dans /job_status
    st = client.get(f"/job_status/{job_id}").get_json()
    assert st["status"] == "completed"
    nodes = st.get("nodes", [])
    assert len(nodes) == 1
    flaky = nodes[0]
    assert flaky["status"] == "done"
    assert flaky["attempt"] == 3
    assert flaky["retry_count"] == 2


def test_bug12_retries_exhaustion_blocks_descendants(client, monkeypatch):
    """
    Vérifie qu'un nœud échouant au-delà de max_retries passe à 'failed'
    et bloque automatiquement ses descendants.
    """
    monkeypatch.setenv("CLUSTER_CI_MAX_NODE_RETRIES", "1")  # Max 1 retry (2 tentatives max)

    client.post("/register_worker", json={
        "worker_id": "worker1", "hostname": "worker1", "cpus": 8, "total_ram_gb": 64.0, "available_storage_gb": 500.0, "role": "worker"
    })

    plan = {
        "nodes": [
            {"name": "failing_step", "deps": [], "resources": {"cpus": 1, "ram_gb": 2.0}, "stale": True},
            {"name": "downstream_step", "deps": ["failing_step"], "resources": {"cpus": 1, "ram_gb": 2.0}, "stale": True},
        ]
    }
    sub = client.post("/submit_job", json={
        "repo": "UNIL-DESI/cluster-ci", "branch": "main", "parallel_mode": 1, "plan": plan
    }).get_json()
    job_id = sub["job_id"]

    scheduler_loop.schedule_iteration()

    # Tentative 1
    t1 = client.post(f"/api/jobs/{job_id}/next_node", json={"worker": "worker1", "runner_id": "r1"}).get_json()
    assert t1.get("node") == "failing_step"
    assert t1.get("attempt") == 1

    # Échec 1 -> retry 1
    t2 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker": "worker1", "runner_id": "r1", "node": "failing_step", "status": "failed", "exit_code": 1
    }).get_json()
    assert t2.get("node") == "failing_step"
    assert t2.get("attempt") == 2

    # Échec 2 -> retries épuisés (max_retries = 1)
    t3 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker": "worker1", "runner_id": "r1", "node": "failing_step", "status": "failed", "exit_code": 1
    }).get_json()
    assert t3.get("action") == "finish"

    # Vérification Observabilité : failing_step is 'failed', downstream_step is 'blocked', job is 'failed'
    st = client.get(f"/job_status/{job_id}").get_json()
    assert st["status"] == "failed"
    nodes_map = {n["name"]: n for n in st["nodes"]}
    assert nodes_map["failing_step"]["status"] == "failed"
    assert nodes_map["failing_step"]["failure_reason"] == "retries_exhausted"
    assert nodes_map["downstream_step"]["status"] == "blocked"


def test_bug12_no_retry_on_host_memory_pressure(client, monkeypatch):
    """
    Vérifie qu'un échec avec HostMemoryPressureExceeded (garde GB10) échoue IMMÉDIATEMENT
    sans aucun retry, même avec retries configurés à 5.
    """
    monkeypatch.setenv("CLUSTER_CI_MAX_NODE_RETRIES", "5")

    client.post("/register_worker", json={
        "worker_id": "worker1", "hostname": "worker1", "cpus": 8, "total_ram_gb": 64.0, "available_storage_gb": 500.0, "role": "worker"
    })

    plan = {
        "nodes": [
            {"name": "gb10_step", "deps": [], "resources": {"cpus": 2, "ram_gb": 4.0}, "stale": True}
        ]
    }
    sub = client.post("/submit_job", json={
        "repo": "UNIL-DESI/cluster-ci", "branch": "main", "parallel_mode": 1, "plan": plan
    }).get_json()
    job_id = sub["job_id"]

    scheduler_loop.schedule_iteration()

    # Tentative 1
    t1 = client.post(f"/api/jobs/{job_id}/next_node", json={"worker": "worker1", "runner_id": "r1"}).get_json()
    assert t1.get("node") == "gb10_step"
    assert t1.get("attempt") == 1

    # Échec par HostMemoryPressureExceeded (137)
    t2 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker": "worker1",
        "runner_id": "r1",
        "node": "gb10_step",
        "status": "failed",
        "exit_code": 137,
        "failure_reason": "HostMemoryPressureExceeded",
        "error_message": "HostMemoryPressureExceeded: MemAvailable below reserve 12GB",
    }).get_json()

    # Ne doit PAS retenter -> action finish direct !
    assert t2.get("action") == "finish"

    # Vérification Observabilité : job et nœud immédiatement failed
    st = client.get(f"/job_status/{job_id}").get_json()
    assert st["status"] == "failed"
    node = st["nodes"][0]
    assert node["status"] == "failed"
    assert node["failure_reason"] == "HostMemoryPressureExceeded"
    assert node["retry_count"] == 0
    assert node["attempt"] == 1


def test_bug12_branch_executor_injection(monkeypatch, tmp_path):
    """
    Vérifie que branch_executor injecte CLUSTER_CI_NODE_ATTEMPT et propage env_vars.
    """
    from src.runner.branch_executor import BranchExecutor, DockerRunner

    class MockDocker(DockerRunner):
        def __init__(self):
            super().__init__()
            self.last_exec_env = {}

        def exec_in_container(self, container_name, command, env=None, user=None, stream_prefix=None):
            self.last_exec_env = dict(env or {})
            return 0, "mock output"

    mock_docker = MockDocker()
    executor = BranchExecutor(
        headnode_url="http://mock-hn",
        job_id="job123",
        runner_id="runner123",
        worker_id="worker123",
        repo_dir=str(tmp_path),
        target_repo="test/repo",
        target_branch="main",
        start_commit="abc",
        docker=mock_docker,
    )
    executor.current_container = "test-container"

    # Exécution avec attempt=2 et variables custom
    code, out = executor.execute_node_in_container(
        node="test_node",
        attempt=2,
        env_vars={"FAIL_STAGE": "test_node", "TOY_DURATION_SEC": "3"},
    )
    assert code == 0
    assert mock_docker.last_exec_env.get("CLUSTER_CI_NODE_ATTEMPT") == "2"
    assert mock_docker.last_exec_env.get("FAIL_STAGE") == "test_node"
    assert mock_docker.last_exec_env.get("TOY_DURATION_SEC") == "3"


def test_bug12_branch_executor_watchdog_lifecycle(monkeypatch, tmp_path):
    """
    Prouve le démarrage et l'arrêt propre de gpu_watchdog.sh (GB10 Guard)
    pendant l'exécution d'un nœud conteneurisé.
    """
    from unittest.mock import MagicMock
    from src.runner.branch_executor import BranchExecutor, DockerRunner
    import subprocess

    class MockDocker(DockerRunner):
        def exec_in_container(self, container_name, command, env=None, user=None, stream_prefix=None):
            return 0, "mock output"

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # Processus actif
    mock_popen = MagicMock(return_value=mock_proc)
    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    monkeypatch.setattr("sys.platform", "linux")

    # Créer le script fictif gpu_watchdog.sh
    watchdog_script = Path(__file__).parent.parent / "src" / "runner" / "gpu_watchdog.sh"
    watchdog_created = False
    if not watchdog_script.exists():
        watchdog_script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        watchdog_created = True

    try:
        executor = BranchExecutor(
            headnode_url="http://mock-hn",
            job_id="job-watchdog",
            runner_id="runner-wd",
            worker_id="worker-wd",
            repo_dir=str(tmp_path),
            target_repo="test/repo",
            target_branch="main",
            start_commit="abc",
            docker=MockDocker(),
        )
        executor.current_container = "test-watchdog-container"

        code, _ = executor.execute_node_in_container(
            node="train",
            attempt=1,
            resources={"vram_gb": 16.0},
        )

        assert code == 0
        assert mock_popen.called
        # Vérifie que le watchdog a été démarré avec les arguments conteneur et vram
        popen_args, popen_kwargs = mock_popen.call_args
        assert popen_args[0][0] == "bash"
        assert "test-watchdog-container" in popen_args[0]
        assert "16" in popen_args[0]
        assert popen_kwargs["env"]["HOST_GUARD_MARKER_FILE"] == "host_guard_killed.marker"

        # Prouve que l'arrêt propre (terminate) a été exécuté dans le bloc finally
        assert mock_proc.terminate.called
        assert mock_proc.wait.called
    finally:
        if watchdog_created and watchdog_script.exists():
            watchdog_script.unlink()


def test_bug9_branch_executor_sanitizer_failure(monkeypatch, tmp_path):
    """
    Prouve que l'échec de workspace_sanitizer (WorkspaceSanitizerError) avant un nœud
    provoque l'échec immédiat du nœud avec failure_reason='WorkspaceSanitizerError'.
    """
    from src.runner.branch_executor import BranchExecutor, DockerRunner
    from src.runner.workspace_sanitizer import WorkspaceSanitizerError

    reported_status = []

    def mock_call_next_node(self, node=None, status=None, duration_s=0.0, exit_code=None,
                            error_message=None, failure_reason=None, cas_transfers=None,
                            missing_deps=None, outputs=None):
        if status:
            reported_status.append({
                "node": node,
                "status": status,
                "exit_code": exit_code,
                "error_message": error_message,
                "failure_reason": failure_reason,
            })
            return {"action": "finish"}
        return {
            "action": "run",
            "node": "faulty_node",
            "image": "python:3.11",
            "attempt": 1,
        }

    monkeypatch.setattr(BranchExecutor, "call_next_node", mock_call_next_node)
    monkeypatch.setattr("src.runner.branch_executor.sync_before_node", lambda **kw: None)

    def mock_sanitizer(ws):
        raise WorkspaceSanitizerError("Hash mismatch on dvc.lock output: data/model.bin")

    monkeypatch.setattr("src.runner.workspace_sanitizer.sanitize_workspace", mock_sanitizer)

    class MockSanitizerDocker(DockerRunner):
        def create_volume(self, volume_name):
            return 0
        def run_container(self, *args, **kwargs):
            return 0
        def exec_in_container(self, *args, **kwargs):
            return 0, ""
        def stop_container(self, *args, **kwargs):
            return 0
        def remove_container(self, *args, **kwargs):
            return 0

    executor = BranchExecutor(
        headnode_url="http://mock-hn",
        job_id="job-sanitizer",
        runner_id="runner-san",
        worker_id="worker-san",
        repo_dir=str(tmp_path),
        target_repo="test/repo",
        target_branch="main",
        start_commit="abc",
        docker=MockSanitizerDocker(),
    )
    executor.current_container = "test-san-container"
    executor.current_image = "python:3.11"

    ret = executor.run()
    assert ret == 0  # run s'arrête sur 'finish'
    assert len(reported_status) == 1
    report = reported_status[0]
    assert report["node"] == "faulty_node"
    assert report["status"] == "failed"
    assert report["exit_code"] == 1
    assert report["failure_reason"] == "WorkspaceSanitizerError"
    assert "Hash mismatch on dvc.lock output" in report["error_message"]

