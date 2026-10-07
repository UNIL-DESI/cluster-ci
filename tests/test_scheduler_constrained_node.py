import json
import uuid
import pytest

from src.scheduler import persistence, scheduler_loop
from src.scheduler.persistence import get_db_conn

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / f"test_cluster_{uuid.uuid4().hex[:8]}.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", db_file)
    persistence.DB_PATH = db_file
    persistence.init_db()
    yield db_file

def test_constrained_node_allocates_extra_worker_even_when_parallelizable_count_reached():
    """
    H5: Teste qu'un nœud prêt avec contrainte de worker spécifique (ex: isipol09)
    permet l'allocation d'un worker supplémentaire même si len(current_active) >= parallelizable_count (1).
    """
    jid = "job-constrained-01"
    w_default = {
        "worker_id": "worker-default",
        "hostname": "worker-default",
        "status": "online",
        "cpus": 16,
        "total_ram": 64.0,
        "available_storage_gb": 100.0,
        "gpus": 0,
        "vram_per_gpu": "[]",
        "service_url": "http://10.0.0.1:8080",
        "arch": "x86_64",
    }
    w_isipol = {
        "worker_id": "isipol09",
        "hostname": "isipol09",
        "status": "online",
        "cpus": 32,
        "total_ram": 128.0,
        "available_storage_gb": 500.0,
        "gpus": 2,
        "vram_per_gpu": "[24.0, 24.0]",
        "service_url": "http://10.0.0.2:8080",
        "arch": "x86_64",
    }

    workers = [w_default, w_isipol]

    with get_db_conn() as conn:
        cursor = conn.cursor()
        for w in workers:
            cursor.execute("""
                INSERT INTO workers (worker_id, hostname, status, cpus, total_ram_gb, available_storage_gb, gpu_count, vram_per_gpu, service_url, arch, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (w["worker_id"], w["hostname"], w["status"], w["cpus"], w["total_ram"], w["available_storage_gb"], w["gpus"], w["vram_per_gpu"], w["service_url"], w["arch"]))

        # Le job a worker-default comme home_worker et active_workers = ["worker-default"]
        cursor.execute("""
            INSERT INTO jobs (job_id, status, parallel_mode, home_worker, active_workers, created_at, username)
            VALUES (?, 'running', 1, 'worker-default', ?, CURRENT_TIMESTAMP, 'alice')
        """, (jid, json.dumps(["worker-default"])))

        # 1 seul nœud prêt exigeant isipol09 (parallelizable_count = 1)
        node_res = {"workers": ["isipol09"], "cpus": 4, "ram_gb": 8.0}
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, resources)
            VALUES (?, 'constrained_stage', 'ready', ?)
        """, (jid, json.dumps(node_res)))
        conn.commit()

    # Exécution d'une itération d'ordonnancement
    scheduler_loop.schedule_iteration()

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT active_workers FROM jobs WHERE job_id = ?", (jid,))
        act = json.loads(cursor.fetchone()[0])
        assert "worker-default" in act
        assert "isipol09" in act, f"isipol09 should have been allocated to job {jid}, got active_workers={act}"
