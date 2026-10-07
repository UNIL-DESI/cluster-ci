import os
import sqlite3
import pytest

from src.scheduler.persistence import init_db, get_job_node, record_runner_heartbeat
from src.scheduler.scheduler_loop import schedule_iteration
from src.scheduler.worker_agent import app as worker_app

@pytest.fixture
def prem_db(tmp_path):
    db_file = tmp_path / "test_preemption.db"
    orig_db = os.environ.get("CLUSTER_DB_PATH")
    os.environ["CLUSTER_DB_PATH"] = str(db_file)
    init_db()
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    yield conn, str(db_file)
    conn.close()
    if orig_db is not None:
        os.environ["CLUSTER_DB_PATH"] = orig_db
    else:
        os.environ.pop("CLUSTER_DB_PATH", None)

def setup_single_worker(conn, worker_id="worker-1", total_ram=32.0):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO workers (
            worker_id, hostname, status, cpus, total_ram_gb, available_ram_gb,
            total_storage_gb, available_storage_gb, last_seen
        ) VALUES (?, 'host-1', 'online', 8, ?, ?, 100.0, 100.0, CURRENT_TIMESTAMP)
    """, (worker_id, total_ram, total_ram))
    conn.commit()

def test_nominal_preemption_high_preempts_low_different_user(prem_db):
    conn, _ = prem_db
    setup_single_worker(conn, "worker-1", total_ram=32.0)
    cursor = conn.cursor()

    # Job Bob avec nœud low en cours d'exécution occupant toute la RAM
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, home_worker, active_workers)
        VALUES ('job-bob', 'bob', 'running', 1, 'worker-1', '["worker-1"]')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, worker_id, runner_id,
            started_at, resources, attempt, retry_count, preempt_count
        ) VALUES (
            'job-bob', 'stage-low', 'running', 'low', 'worker-1', 'runner-bob-1',
            CURRENT_TIMESTAMP, '{"cpus": 4, "ram_gb": 20.0}', 1, 0, 0
        )
    """)
    conn.commit()
    record_runner_heartbeat('job-bob', 'runner-bob-1', 'worker-1', 'stage-low')

    # Job Alice avec nœud high en ready demandant 20GB (ne peut pas packer)
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode)
        VALUES ('job-alice', 'alice', 'pending', 1)
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, resources, attempt, retry_count
        ) VALUES (
            'job-alice', 'stage-high', 'ready', 'high', '{"cpus": 4, "ram_gb": 20.0}', 0, 0
        )
    """)
    conn.commit()

    # Exécution de l'itération d'ordonnancement
    schedule_iteration()

    # Vérification : stage-low de Bob doit avoir été basculé en état 'preempting'
    bob_node = get_job_node("job-bob", "stage-low")
    assert bob_node["status"] == "preempting"
    assert bob_node["preempted_by"] == "job-alice:stage-high"

    # Simulation de la confirmation d'arrêt du worker via /update_job_status
    from src.scheduler.headnode_service import app as head_app
    client = head_app.test_client()
    resp = client.post("/update_job_status", json={
        "job_id": "job-bob",
        "status": "failed",
        "exit_code": 137,
        "worker_id": "worker-1",
        "runner_id": "runner-bob-1"
    })
    assert resp.status_code == 200

    bob_node = get_job_node("job-bob", "stage-low")
    assert bob_node["status"] == "ready"
    assert bob_node["preempt_count"] == 1
    assert bob_node["retry_count"] == 0  # Inchangé !
    assert bob_node["attempt"] == 1      # Inchangé !
    assert bob_node["failure_reason"] is None

    # Et job-alice doit avoir reçu worker-1 comme home_worker libéré
    cursor = conn.cursor()
    cursor.execute("SELECT home_worker FROM jobs WHERE job_id = 'job-alice'")
    assert cursor.fetchone()["home_worker"] == "worker-1"

    # Le worker exécute le nœud via handle_next_node
    from src.scheduler.scheduler_loop import handle_next_node
    handle_next_node({"job_id": "job-alice", "worker_id": "worker-1", "runner_id": "runner-alice-1"})

    alice_node = get_job_node("job-alice", "stage-high")
    assert alice_node["status"] == "running"
    assert alice_node["worker_id"] == "worker-1"

def test_anti_starvation_cap_at_3(prem_db):
    conn, _ = prem_db
    setup_single_worker(conn, "worker-1", total_ram=32.0)
    cursor = conn.cursor()

    # Job Bob avec nœud low ayant DÉJÀ atteint preempt_count = 3
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, home_worker, active_workers)
        VALUES ('job-bob', 'bob', 'running', 1, 'worker-1', '["worker-1"]')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, worker_id, runner_id,
            started_at, resources, attempt, retry_count, preempt_count
        ) VALUES (
            'job-bob', 'stage-low', 'running', 'low', 'worker-1', 'runner-bob-3',
            CURRENT_TIMESTAMP, '{"cpus": 4, "ram_gb": 20.0}', 1, 0, 3
        )
    """)
    conn.commit()
    record_runner_heartbeat('job-bob', 'runner-bob-3', 'worker-1', 'stage-low')

    # Job Alice avec nœud high
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode)
        VALUES ('job-alice', 'alice', 'pending', 1)
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, resources
        ) VALUES (
            'job-alice', 'stage-high', 'ready', 'high', '{"cpus": 4, "ram_gb": 20.0}'
        )
    """)
    conn.commit()

    schedule_iteration()

    # Le nœud de Bob ne doit PAS être préempté car preempt_count == 3 (plafond atteint)
    bob_node = get_job_node("job-bob", "stage-low")
    assert bob_node["status"] == "running"
    assert bob_node["preempt_count"] == 3

    # Le nœud d'Alice reste en ready car aucune machine n'est libérée
    alice_node = get_job_node("job-alice", "stage-high")
    assert alice_node["status"] == "ready"

def test_immunity_for_normal_and_high_nodes(prem_db):
    conn, _ = prem_db
    setup_single_worker(conn, "worker-1", total_ram=32.0)
    cursor = conn.cursor()

    # Job Bob exécutant un nœud 'normal'
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, home_worker, active_workers)
        VALUES ('job-bob', 'bob', 'running', 1, 'worker-1', '["worker-1"]')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, worker_id, runner_id,
            started_at, resources, attempt, retry_count, preempt_count
        ) VALUES (
            'job-bob', 'stage-normal', 'running', 'normal', 'worker-1', 'runner-bob-norm',
            CURRENT_TIMESTAMP, '{"cpus": 4, "ram_gb": 20.0}', 1, 0, 0
        )
    """)
    conn.commit()
    record_runner_heartbeat('job-bob', 'runner-bob-norm', 'worker-1', 'stage-normal')

    # Job Alice demandant un nœud 'high'
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode)
        VALUES ('job-alice', 'alice', 'pending', 1)
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, resources
        ) VALUES (
            'job-alice', 'stage-high', 'ready', 'high', '{"cpus": 4, "ram_gb": 20.0}'
        )
    """)
    conn.commit()

    schedule_iteration()

    # Le nœud normal de Bob est strictement immunisé contre la préemption
    bob_node = get_job_node("job-bob", "stage-normal")
    assert bob_node["status"] == "running"

    alice_node = get_job_node("job-alice", "stage-high")
    assert alice_node["status"] == "ready"

def test_strict_inter_user_no_intra_user_preemption(prem_db):
    conn, _ = prem_db
    setup_single_worker(conn, "worker-1", total_ram=32.0)
    cursor = conn.cursor()

    # Même utilisateur Alice pour les deux jobs (intra-user)
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, home_worker, active_workers)
        VALUES ('job-alice-1', 'alice', 'running', 1, 'worker-1', '["worker-1"]')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, worker_id, runner_id,
            started_at, resources, attempt, retry_count, preempt_count
        ) VALUES (
            'job-alice-1', 'stage-low', 'running', 'low', 'worker-1', 'runner-alice-1',
            CURRENT_TIMESTAMP, '{"cpus": 4, "ram_gb": 20.0}', 1, 0, 0
        )
    """)
    conn.commit()
    record_runner_heartbeat('job-alice-1', 'runner-alice-1', 'worker-1', 'stage-low')

    # Alice lance un job high
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode)
        VALUES ('job-alice-2', 'alice', 'pending', 1)
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, resources
        ) VALUES (
            'job-alice-2', 'stage-high', 'ready', 'high', '{"cpus": 4, "ram_gb": 20.0}'
        )
    """)
    conn.commit()

    schedule_iteration()

    # Jamais de préemption entre jobs du même utilisateur
    alice_node_1 = get_job_node("job-alice-1", "stage-low")
    assert alice_node_1["status"] == "running"

def test_most_recent_victim_selected_first(prem_db):
    conn, _ = prem_db
    # Deux workers
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO workers (worker_id, hostname, status, cpus, total_ram_gb, available_ram_gb, total_storage_gb, available_storage_gb, last_seen)
        VALUES ('worker-A', 'host-a', 'online', 8, 32.0, 32.0, 100.0, 100.0, CURRENT_TIMESTAMP),
               ('worker-B', 'host-b', 'online', 8, 32.0, 32.0, 100.0, 100.0, CURRENT_TIMESTAMP)
    """)

    # Bob a 2 nœuds low en cours : un ancien (10:00) sur worker-A et un récent (10:30) sur worker-B
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, home_worker, active_workers)
        VALUES ('job-bob', 'bob', 'running', 1, 'worker-A', '["worker-A", "worker-B"]')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (job_id, node_name, status, scheduling_priority, worker_id, runner_id, started_at, resources)
        VALUES ('job-bob', 'stage-older', 'running', 'low', 'worker-A', 'runner-old', '2026-10-01 10:00:00', '{"cpus": 4, "ram_gb": 20.0}'),
               ('job-bob', 'stage-newer', 'running', 'low', 'worker-B', 'runner-new', '2026-10-01 10:30:00', '{"cpus": 4, "ram_gb": 20.0}')
    """)
    conn.commit()
    record_runner_heartbeat('job-bob', 'runner-old', 'worker-A', 'stage-older')
    record_runner_heartbeat('job-bob', 'runner-new', 'worker-B', 'stage-newer')

    # Alice a un nœud high qui nécessite une machine entière (20.0 GB)
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode)
        VALUES ('job-alice', 'alice', 'pending', 1)
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, resources
        ) VALUES (
            'job-alice', 'stage-high', 'ready', 'high', '{"cpus": 4, "ram_gb": 20.0}')
    """)
    conn.commit()

    schedule_iteration()

    # La victime sélectionnée DOIT être stage-newer (le plus récent, started_at DESC)
    node_newer = get_job_node("job-bob", "stage-newer")
    node_older = get_job_node("job-bob", "stage-older")

    assert node_newer["status"] == "preempting"
    assert node_newer["preempted_by"] == "job-alice:stage-high"

    # Le plus ancien continue de tourner
    assert node_older["status"] == "running"

def test_worker_agent_preempt_runner_endpoint(monkeypatch):
    import src.scheduler.worker_agent as worker_agent_mod
    monkeypatch.setattr(worker_agent_mod, "CLUSTER_TOKEN", "test-secret-token")

    client = worker_app.test_client()

    # 1. Requête sans Authorization -> 401
    resp_unauth = client.post("/api/worker/preempt_runner/test-runner-123")
    assert resp_unauth.status_code == 401
    assert resp_unauth.get_json()["error"] == "Unauthorized"

    # 2. Requête avec mauvais token -> 401
    resp_bad = client.post(
        "/api/worker/preempt_runner/test-runner-123",
        headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp_bad.status_code == 401

    # 3. Requête avec token valide -> 200
    resp_ok = client.post(
        "/api/worker/preempt_runner/test-runner-123",
        headers={"Authorization": "Bearer test-secret-token"}
    )
    assert resp_ok.status_code == 200
    data = resp_ok.get_json()
    assert data["status"] == "preempted"
    assert data["runner_id"] == "test-runner-123"


def test_docker_runner_labels_and_worker_preempt_cleanup_by_label(monkeypatch):
    import subprocess
    from src.runner.branch_executor import DockerRunner
    from src.scheduler.worker_agent import _async_runner_preempt_cleanup

    # 1. Vérification de la commande émise par DockerRunner.run_container
    captured_cmds = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        captured_cmds.append(cmd)
        class DummyRes:
            returncode = 0
            stdout = ""
            stderr = ""
        return DummyRes()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    runner = DockerRunner(docker_cmd="docker")
    labels = {
        "cluster-ci.runner-id": "runner-xyz-999",
        "cluster-ci.job-id": "job-12345",
    }
    ret = runner.run_container(
        image="test-img:latest",
        container_name="cluster-job-job12345-test-img",
        home_volume="vol-home",
        repo_dir="/tmp/repo",
        base_dir="/tmp/base",
        labels=labels,
    )
    assert ret == 0
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert "docker" in cmd
    assert "run" in cmd
    assert "--label" in cmd
    assert "cluster-ci.runner-id=runner-xyz-999" in cmd
    assert "cluster-ci.job-id=job-12345" in cmd

    # 2. Vérification que _async_runner_preempt_cleanup interroge bien docker ps avec le label canonique
    ps_filter_calls = []
    removed_containers = []

    def fake_cleanup_run(cmd, *args, **kwargs):
        if "docker" in cmd and "ps" in cmd:
            ps_filter_calls.append(cmd)
            class PsRes:
                returncode = 0
                stdout = "cluster-job-job12345-test-img\n"
                stderr = ""
            return PsRes()
        class OtherRes:
            returncode = 0
            stdout = ""
            stderr = ""
        return OtherRes()

    monkeypatch.setattr(subprocess, "run", fake_cleanup_run)
    monkeypatch.setattr("src.scheduler.worker_agent.safe_docker_rm_f", lambda c, timeout=8: removed_containers.extend(c))
    monkeypatch.setattr("src.scheduler.worker_agent.purge_ollama_vram_on_host", lambda: None)
    monkeypatch.setattr("src.scheduler.worker_agent.kill_dvc_viewer_processes", lambda: None)

    _async_runner_preempt_cleanup("runner-xyz-999", process_to_kill=None, grace_period_s=0)

    assert len(ps_filter_calls) >= 1
    first_ps = ps_filter_calls[0]
    assert "--filter" in first_ps
    assert "label=cluster-ci.runner-id=runner-xyz-999" in first_ps
    assert "cluster-job-job12345-test-img" in removed_containers


def test_preempting_node_interception_update_job_status(prem_db):
    from src.scheduler.headnode_service import app as head_app
    conn, _ = prem_db
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, home_worker, active_workers)
        VALUES ('job-victim', 'bob', 'running', 1, 'worker-1', '["worker-1"]')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, worker_id, runner_id,
            started_at, resources, attempt, retry_count, preempt_count, preempted_by
        ) VALUES (
            'job-victim', 'stage-preempted', 'preempting', 'low', 'worker-1', 'runner-victim-1',
            CURRENT_TIMESTAMP, '{"cpus": 4, "ram_gb": 20.0}', 1, 0, 0, 'job-pri:high-node'
        )
    """)
    conn.commit()

    # Le worker arrêté envoie /update_job_status avec exit_code 137 (SIGKILL/OOM)
    client = head_app.test_client()
    resp = client.post("/update_job_status", json={
        "job_id": "job-victim",
        "status": "failed",
        "exit_code": 137,
        "error_message": "Process terminated by SIGKILL",
        "worker_id": "worker-1",
        "runner_id": "runner-victim-1"
    })
    assert resp.status_code == 200

    # Vérification : le nœud DOIT être intercepté comme préempté, pas comme failed / OOM
    node = get_job_node("job-victim", "stage-preempted")
    assert node["status"] == "ready"
    assert node["preempt_count"] == 1
    assert node["retry_count"] == 0
    assert node["attempt"] == 1
    assert node["failure_reason"] is None
    assert node["preempted_by"] == "job-pri:high-node"


def test_preempting_node_success_prevails(prem_db):
    from src.scheduler.headnode_service import app as head_app
    conn, _ = prem_db
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, home_worker, active_workers)
        VALUES ('job-lucky', 'bob', 'running', 1, 'worker-1', '["worker-1"]')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, worker_id, runner_id,
            started_at, resources, attempt, retry_count, preempt_count, preempted_by
        ) VALUES (
            'job-lucky', 'stage-lucky', 'preempting', 'low', 'worker-1', 'runner-lucky-1',
            CURRENT_TIMESTAMP, '{"cpus": 4, "ram_gb": 20.0}', 1, 0, 0, 'job-pri:high-node'
        )
    """)
    conn.commit()

    # Le runner a fini avec succès (exit_code 0) juste avant l'arrêt
    client = head_app.test_client()
    resp = client.post("/update_job_status", json={
        "job_id": "job-lucky",
        "status": "completed",
        "exit_code": 0,
        "worker_id": "worker-1",
        "runner_id": "runner-lucky-1"
    })
    assert resp.status_code == 200

    node = get_job_node("job-lucky", "stage-lucky")
    assert node["status"] == "done"
    assert node["exit_code"] == 0


@pytest.mark.parametrize("exit_code", [-15, -9, 137, 143])
def test_preempting_node_requeues_and_keeps_job_active(prem_db, exit_code):
    from src.scheduler.headnode_service import app as head_app
    conn, _ = prem_db
    cursor = conn.cursor()

    job_id = f"job-victim-{abs(exit_code)}"
    runner_id = f"runner-victim-{abs(exit_code)}"
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, home_worker, active_workers)
        VALUES (?, 'bob', 'running', 1, 'worker-1', '["worker-1"]')
    """, (job_id,))
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, worker_id, runner_id,
            started_at, resources, attempt, retry_count, preempt_count, preempted_by
        ) VALUES (
            ?, 'stage-preempted', 'preempting', 'low', 'worker-1', ?,
            CURRENT_TIMESTAMP, '{"cpus": 4, "ram_gb": 20.0}', 1, 0, 0, 'job-pri:high-node'
        )
    """, (job_id, runner_id))
    conn.commit()

    client = head_app.test_client()
    resp = client.post("/update_job_status", json={
        "job_id": job_id,
        "status": "failed",
        "exit_code": exit_code,
        "error_message": f"Process terminated with exit code {exit_code}",
        "worker_id": "worker-1",
        "runner_id": runner_id
    })
    assert resp.status_code == 200

    # Vérification nœud : requeue en 'ready', preempt_count incrementé, retry intact
    node = get_job_node(job_id, "stage-preempted")
    assert node["status"] == "ready"
    assert node["preempt_count"] == 1
    assert node["retry_count"] == 0
    assert node["attempt"] == 1
    assert node["failure_reason"] is None
    assert node["preempted_by"] == "job-pri:high-node"

    # Vérification job : le job doit TOUJOURS être actif ('running'), pas annulé ni échoué
    cursor.execute("SELECT status, retry_count, failure_reason FROM jobs WHERE job_id = ?", (job_id,))
    job_row = cursor.fetchone()
    assert job_row["status"] == "running"
    assert job_row["retry_count"] == 0 or job_row["retry_count"] is None
    assert job_row["failure_reason"] is None


def test_non_preempting_node_user_cancellation_preserves_behavior(prem_db):
    from src.scheduler.headnode_service import app as head_app
    conn, _ = prem_db
    cursor = conn.cursor()

    job_id = "job-user-cancel"
    runner_id = "runner-cancel-1"
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, home_worker, active_workers)
        VALUES (?, 'bob', 'running', 1, 'worker-1', '["worker-1"]')
    """, (job_id,))
    cursor.execute("""
        INSERT INTO job_nodes (
            job_id, node_name, status, scheduling_priority, worker_id, runner_id,
            started_at, resources, attempt, retry_count, preempt_count
        ) VALUES (
            ?, 'stage-running', 'running', 'normal', 'worker-1', ?,
            CURRENT_TIMESTAMP, '{"cpus": 4, "ram_gb": 20.0}', 1, 0, 0
        )
    """, (job_id, runner_id))
    conn.commit()

    # Annulation externe utilisateur avec exit_code -15 sur un nœud NON-preempting
    client = head_app.test_client()
    resp = client.post("/update_job_status", json={
        "job_id": job_id,
        "status": "failed",
        "exit_code": -15,
        "error_message": "User sent SIGTERM",
        "worker_id": "worker-1",
        "runner_id": runner_id
    })
    assert resp.status_code == 200

    # Vérification job : doit être routé vers cancel_job_cleanly et annulé/échoué
    cursor.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
    job_row = cursor.fetchone()
    assert job_row["status"] in ("failed", "cancelled")



