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

    # Vérification : stage-low de Bob doit avoir été préempté
    bob_node = get_job_node("job-bob", "stage-low")
    assert bob_node["status"] == "ready"
    assert bob_node["preempt_count"] == 1
    assert bob_node["retry_count"] == 0  # Inchangé !
    assert bob_node["attempt"] == 1      # Inchangé !
    assert bob_node["failure_reason"] is None
    assert bob_node["preempted_by"] == "job-alice:stage-high"
    assert bob_node["worker_id"] is None
    assert bob_node["runner_id"] is None

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

    assert node_newer["status"] == "ready"
    assert node_newer["preempt_count"] == 1

    # Le plus ancien continue de tourner
    assert node_older["status"] == "running"

def test_worker_agent_preempt_runner_endpoint():
    client = worker_app.test_client()
    resp = client.post("/api/worker/preempt_runner/test-runner-123")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "preempted"
    assert data["runner_id"] == "test-runner-123"
