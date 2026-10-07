import os
import sqlite3
import pytest

from src.scheduler.persistence import init_db
from src.scheduler.scheduling_order import (
    scheduling_node_sort_key,
    get_user_machine_counts,
    get_priority_rank,
    format_waiting_time,
)
from src.scheduler.headnode_service import get_worker_queues

@pytest.fixture
def sched_db(tmp_path):
    db_file = tmp_path / "test_sched_order.db"
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

def test_priority_ranking():
    assert get_priority_rank("high") == 0
    assert get_priority_rank("HIGH") == 0
    assert get_priority_rank("normal") == 1
    assert get_priority_rank("NORMAL") == 1
    assert get_priority_rank(None) == 1
    assert get_priority_rank("low") == 2
    assert get_priority_rank("LOW") == 2

def test_format_waiting_time():
    assert format_waiting_time(10) == "10s"
    assert format_waiting_time(75) == "1m 15s"
    assert format_waiting_time(120) == "2m"
    assert format_waiting_time(3660) == "1h 01m"

def test_scheduling_node_sort_key_priority_and_fairness():
    user_counts = {"alice": 0, "bob": 2}

    # high priority vs normal vs low
    node_high = {"scheduling_priority": "high", "username": "bob", "status": "ready", "created_at": "2026-10-01 10:00:00", "node_name": "n1"}
    node_normal_alice = {"scheduling_priority": "normal", "username": "alice", "status": "ready", "created_at": "2026-10-01 10:00:00", "node_name": "n2"}
    node_normal_bob = {"scheduling_priority": "normal", "username": "bob", "status": "ready", "created_at": "2026-10-01 09:00:00", "node_name": "n3"}
    node_low = {"scheduling_priority": "low", "username": "alice", "status": "ready", "created_at": "2026-10-01 08:00:00", "node_name": "n4"}

    key_high = scheduling_node_sort_key(node_high, user_machine_counts=user_counts)
    key_alice = scheduling_node_sort_key(node_normal_alice, user_machine_counts=user_counts)
    key_bob = scheduling_node_sort_key(node_normal_bob, user_machine_counts=user_counts)
    key_low = scheduling_node_sort_key(node_low, user_machine_counts=user_counts)

    # 1. High passe avant normal (même si Bob a déjà 2 machines)
    assert key_high < key_alice
    assert key_high < key_bob

    # 2. Pour une même priorité (normal), Alice (0 machine) passe avant Bob (2 machines) même si Bob est plus ancien
    assert key_alice < key_bob

    # 3. Normal passe avant low
    assert key_bob < key_low

def test_exact_parity_between_workers_queue_and_scheduler(sched_db):
    conn, _ = sched_db
    cursor = conn.cursor()

    # Deux workers en ligne
    cursor.execute("""
        INSERT INTO workers (worker_id, hostname, status, cpus, total_ram_gb, available_ram_gb, total_storage_gb, available_storage_gb, last_seen)
        VALUES ('worker-A', 'host-a', 'online', 8, 32.0, 32.0, 100.0, 100.0, CURRENT_TIMESTAMP),
               ('worker-B', 'host-b', 'online', 8, 32.0, 32.0, 100.0, 100.0, CURRENT_TIMESTAMP)
    """)

    # Utilisateur Bob détient déjà worker-A sur un job en cours
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, created_at, home_worker, active_workers)
        VALUES ('job-bob-running', 'bob', 'running', 1, '2026-10-01 08:00:00', 'worker-A', '["worker-A"]')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (job_id, node_name, status, worker_id, scheduling_priority)
        VALUES ('job-bob-running', 'stage-running', 'running', 'worker-A', 'normal')
    """)

    # Jobs en attente :
    # 1. Job Alice (0 machine) avec priorité normal
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, created_at, scheduling_priority)
        VALUES ('job-alice', 'alice', 'pending', 1, '2026-10-01 10:00:00', 'normal')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (job_id, node_name, status, scheduling_priority, resources, priority)
        VALUES ('job-alice', 'stage-alice', 'ready', 'normal', '{"cpus": 2, "ram_gb": 4.0}', 1.0)
    """)

    # 2. Job Bob (1 machine active) avec priorité normal mais plus ancien
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, created_at, scheduling_priority)
        VALUES ('job-bob-queued', 'bob', 'pending', 1, '2026-10-01 09:00:00', 'normal')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (job_id, node_name, status, scheduling_priority, resources, priority)
        VALUES ('job-bob-queued', 'stage-bob', 'ready', 'normal', '{"cpus": 2, "ram_gb": 4.0}', 1.0)
    """)

    # 3. Job Charlie (0 machine) avec priorité high
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, created_at, scheduling_priority)
        VALUES ('job-charlie', 'charlie', 'pending', 1, '2026-10-01 11:00:00', 'high')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (job_id, node_name, status, scheduling_priority, resources, priority)
        VALUES ('job-charlie', 'stage-charlie', 'ready', 'high', '{"cpus": 2, "ram_gb": 4.0}', 1.0)
    """)

    conn.commit()

    # Vérification des comptes de machines utilisateurs
    user_counts = get_user_machine_counts(conn)
    assert user_counts.get("bob") == 1
    assert user_counts.get("alice", 0) == 0
    assert user_counts.get("charlie", 0) == 0

    # Récupération des files d'attente pour worker-B via get_worker_queues
    queues = get_worker_queues(conn=conn, target_worker_id="worker-B")
    queue_b = queues["worker-B"]

    # Ordre attendu :
    # 1. Charlie (priorité 'high')
    # 2. Alice (priorité 'normal', 0 machine occupée)
    # 3. Bob (priorité 'normal', mais détient déjà 1 machine physique)
    assert len(queue_b) == 3
    assert queue_b[0]["node_name"] == "stage-charlie"
    assert queue_b[0]["scheduling_priority"] == "high"
    assert queue_b[1]["node_name"] == "stage-alice"
    assert queue_b[1]["scheduling_priority"] == "normal"
    assert queue_b[2]["node_name"] == "stage-bob"
    assert queue_b[2]["scheduling_priority"] == "normal"


def test_strict_queue_parity_with_active_workers_and_home_worker(sched_db):
    from src.scheduler.scheduler_loop import schedule_iteration, handle_next_node
    conn, _ = sched_db
    cursor = conn.cursor()

    # Deux workers en ligne
    cursor.execute("""
        INSERT INTO workers (worker_id, hostname, status, cpus, total_ram_gb, available_ram_gb, total_storage_gb, available_storage_gb, last_seen, assigned_job_id)
        VALUES ('worker-A', 'host-a', 'online', 8, 32.0, 4.0, 100.0, 100.0, CURRENT_TIMESTAMP, 'job-1-active'),
               ('worker-B', 'host-b', 'online', 8, 32.0, 32.0, 100.0, 100.0, CURRENT_TIMESTAMP, NULL)
    """)

    # Job 1 : Déjà assigné à worker-A (home_worker='worker-A', active_workers='["worker-A"]')
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, created_at, started_at, home_worker, active_workers)
        VALUES ('job-1-active', 'bob', 'running', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'worker-A', '["worker-A"]')
    """)
    cursor.execute("""
        INSERT INTO job_nodes (job_id, node_name, status, worker_id, runner_id, scheduling_priority, resources)
        VALUES ('job-1-active', 'stage-1-running', 'running', 'worker-A', 'runner-1-A', 'normal', '{"cpus": 6, "ram_gb": 28.0}')
    """)
    # Job 1 a un deuxième nœud prêt à tourner
    cursor.execute("""
        INSERT INTO job_nodes (job_id, node_name, status, scheduling_priority, resources, priority)
        VALUES ('job-1-active', 'stage-1-ready', 'ready', 'normal', '{"cpus": 2, "ram_gb": 4.0}', 1.0)
    """)
    conn.commit()
    from src.scheduler.persistence import record_runner_heartbeat
    record_runner_heartbeat('job-1-active', 'runner-1-A', 'worker-A', 'stage-1-running')

    # Job 2 : Pending, demandant 10GB de RAM (ne peut pas entrer sur worker-A car 28GB+10GB > 32GB)
    cursor.execute("""
        INSERT INTO jobs (job_id, username, status, parallel_mode, created_at)
        VALUES ('job-2-pending', 'alice', 'pending', 1, CURRENT_TIMESTAMP)
    """)
    cursor.execute("""
        INSERT INTO job_nodes (job_id, node_name, status, scheduling_priority, resources, priority)
        VALUES ('job-2-pending', 'stage-2-ready', 'ready', 'normal', '{"cpus": 2, "ram_gb": 10.0}', 1.0)
    """)
    conn.commit()

    # 1. Vérification parité dans l'API de file (/api/workers/queues)
    queues = get_worker_queues(conn=conn)
    queue_a = queues["worker-A"]
    queue_b = queues["worker-B"]

    # worker-A a accès aux deux jobs (le sien + le pending)
    a_nodes = [item["node_name"] for item in queue_a]
    assert "stage-1-ready" in a_nodes
    assert "stage-2-ready" in a_nodes

    # worker-B NE DOIT PAS voir stage-1-ready (réservé aux machines actives du job-1)
    b_nodes = [item["node_name"] for item in queue_b]
    assert "stage-1-ready" not in b_nodes
    assert "stage-2-ready" in b_nodes

    # 2. Vérification parité avec le scheduler : l'itération d'ordonnancement attribue worker-B à job-2-pending
    schedule_iteration()

    cursor.execute("SELECT home_worker FROM jobs WHERE job_id = 'job-2-pending'")
    j2_home = cursor.fetchone()[0]
    assert j2_home == "worker-B"

