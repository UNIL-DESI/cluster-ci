import os
import sys
import json
import uuid
import pytest

# Ensure scheduler directory is on sys.path
sched_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "scheduler")
if sched_dir not in sys.path:
    sys.path.insert(0, sched_dir)

import persistence
import scheduler_loop
from src.config.defaults import MAX_WORKERS_PER_JOB
from src.scheduler.scheduling_order import extra_worker_sort_key
from persistence import get_db_conn

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / f"test_cluster_{uuid.uuid4().hex[:8]}.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", db_file)
    persistence.DB_PATH = db_file
    persistence.init_db()
    yield db_file

def test_h4_default_max_workers_per_job_is_two():
    """H4: Vérifie que MAX_WORKERS_PER_JOB est à 2 par défaut."""
    assert MAX_WORKERS_PER_JOB == 2


def test_h4_extra_worker_sort_key_order():
    """
    H4: Vérifie l'ordre unifié : priorité > équité machine > équité user > FIFO.
    """
    job_high = {"job_id": "j-high", "scheduling_priority": "high", "username": "alice", "created_at": "2026-01-01 10:00:00"}
    job_normal = {"job_id": "j-norm", "scheduling_priority": "normal", "username": "bob", "created_at": "2026-01-01 09:00:00"}
    job_norm_2 = {"job_id": "j-norm2", "scheduling_priority": "normal", "username": "charlie", "created_at": "2026-01-01 08:00:00"}

    # 1. High passe avant Normal même si Normal a moins de machines
    key_high = extra_worker_sort_key(job_high, current_active_workers=["w1"], user_machine_counts={"alice": 1, "bob": 0})
    key_norm = extra_worker_sort_key(job_normal, current_active_workers=[], user_machine_counts={"alice": 1, "bob": 0})
    assert key_high < key_norm, "High priority job must precede normal priority job"

    # 2. À priorité égale (normal), équité machine : 1 machine passe avant 2 machines
    key_norm_1_worker = extra_worker_sort_key(job_normal, current_active_workers=["w1"], user_machine_counts={"bob": 1, "charlie": 0})
    key_norm_2_workers = extra_worker_sort_key(job_norm_2, current_active_workers=["w1", "w2"], user_machine_counts={"bob": 1, "charlie": 0})
    assert key_norm_1_worker < key_norm_2_workers, "Job with fewer active machines must be preferred"

    # 3. À priorité et nombre de machines égaux, équité utilisateur : 0 machine détenue avant 1
    key_user_0 = extra_worker_sort_key(job_norm_2, current_active_workers=["w1"], user_machine_counts={"bob": 1, "charlie": 0})
    key_user_1 = extra_worker_sort_key(job_normal, current_active_workers=["w1"], user_machine_counts={"bob": 1, "charlie": 0})
    assert key_user_0 < key_user_1, "User with 0 assigned machines must precede user with 1"


def test_h4_max_workers_ceiling_enforced_in_scheduling():
    """
    H4: Vérifie que le scheduler plafonne à MAX_WORKERS_PER_JOB (2) l'attribution de machines à un job.
    """
    jid = "job-ceiling-01"
    workers = []
    for i in range(1, 5):
        workers.append({
            "worker_id": f"w{i}",
            "hostname": f"w{i}",
            "status": "online",
            "cpus": 8,
            "total_ram_gb": 32.0,
            "available_storage_gb": 100.0,
            "gpu_count": 0,
            "vram_per_gpu": "[]",
            "service_url": f"http://10.0.0.{i}:8080",
            "arch": "x86_64",
        })

    with get_db_conn() as conn:
        cursor = conn.cursor()
        for w in workers:
            cursor.execute("""
                INSERT INTO workers (worker_id, hostname, status, cpus, total_ram_gb, available_storage_gb, gpu_count, vram_per_gpu, service_url, arch, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (w["worker_id"], w["hostname"], w["status"], w["cpus"], w["total_ram_gb"], w["available_storage_gb"], w["gpu_count"], w["vram_per_gpu"], w["service_url"], w["arch"]))

        # Le job démarre avec w1 comme home_worker et 4 nœuds prêts indépendants
        cursor.execute("""
            INSERT INTO jobs (job_id, status, parallel_mode, home_worker, active_workers, created_at, username)
            VALUES (?, 'running', 1, 'w1', ?, CURRENT_TIMESTAMP, 'alice')
        """, (jid, json.dumps(["w1"])))

        for k in range(1, 5):
            res = {"cpus": 2, "ram_gb": 4.0}
            cursor.execute("""
                INSERT INTO job_nodes (job_id, node_name, status, resources)
                VALUES (?, ?, 'ready', ?)
            """, (jid, f"stage_{k}", json.dumps(res)))
        conn.commit()

    # Déroulement du scheduler
    scheduler_loop.schedule_iteration()

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT active_workers FROM jobs WHERE job_id = ?", (jid,))
        act = json.loads(cursor.fetchone()[0])
        # w1 (home) + 1 worker supplémentaire = 2 workers maximum (plafond MAX_WORKERS_PER_JOB=2)
        assert len(act) <= 2, f"Expected at most 2 workers allocated to job {jid}, got {len(act)}: {act}"
