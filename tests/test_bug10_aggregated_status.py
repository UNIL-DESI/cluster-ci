"""
test_bug10_aggregated_status.py - Validation du Bug 10 :
Statut de job dérivé strictement de l'agrégat de tous ses nœuds DAG
(interdiction formelle de passer à completed alors que d'autres nœuds tournent).
"""

import os
import sys
import uuid
import pytest

sched_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "scheduler")
if sched_dir not in sys.path:
    sys.path.insert(0, sched_dir)

import persistence
import scheduler_loop
import headnode_service


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Initialise une base SQLite temporaire et isolée."""
    db_file = str(tmp_path / f"test_cluster_bug10_{uuid.uuid4().hex[:8]}.db")
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


def test_bug10_job_remains_running_when_first_worker_reports_completed(client):
    """
    Vérifie qu'un job DAG à 2 nœuds (prep -> train) reste strictement 'running'
    lorsque le premier worker termine 'prep' et envoie /update_job_status completed.
    Seul le worker appelant est libéré, et le job ne passe à 'completed' que lorsque
    tous les nœuds sont 'done'.
    """
    # 1. Enregistrer un worker
    reg_resp = client.post("/register_worker", json={
        "worker_id": "worker1",
        "hostname": "worker1",
        "service_url": "http://127.0.0.1:5001",
        "cpus": 8,
        "total_ram_gb": 64.0,
        "available_storage_gb": 500.0,
        "role": "worker",
    })
    assert reg_resp.status_code == 200

    # 2. Soumettre un job DAG à 2 nœuds
    plan = {
        "nodes": [
            {
                "name": "prep",
                "deps": [],
                "resources": {"cpus": 2, "ram_gb": 4.0},
                "stale": True,
            },
            {
                "name": "train",
                "deps": ["prep"],
                "resources": {"cpus": 2, "ram_gb": 4.0},
                "stale": True,
            },
        ],
        "defaults": {"image": "test-image:latest"},
    }
    sub_resp = client.post("/submit_job", json={
        "repo": "UNIL-DESI/cluster-ci",
        "branch": "main",
        "parallel_mode": 1,
        "plan": plan,
    })
    assert sub_resp.status_code == 200
    job_id = sub_resp.get_json()["job_id"]

    # Assigner home_worker via schedule_iteration
    scheduler_loop.schedule_iteration()

    # 3. Worker 1 demande le premier nœud via /next_node
    next1 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker_id": "worker1",
        "runner_id": "runner1",
        "node": None,
        "status": None,
    }).get_json()
    assert next1.get("action") in ("run", "switch_image")
    assert next1.get("node") == "prep"

    # Vérifier que le job est 'running' et que prep est 'running'
    st1 = client.get(f"/job_status/{job_id}").get_json()
    assert st1["status"] == "running"

    # 4. Worker 1 termine 'prep' et appelle /next_node avec done
    next2 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker_id": "worker1",
        "runner_id": "runner1",
        "node": "prep",
        "status": "done",
        "duration_s": 2.5,
        "exit_code": 0,
    }).get_json()
    # next2 attribue 'train' car train est maintenant ready
    assert next2.get("action") in ("run", "switch_image")
    assert next2.get("node") == "train"

    # 5. Simuler un worker runner annonçant prématurément /update_job_status completed
    # (par exemple un runner qui a terminé sa tranche ou un appel d'ancien format)
    update_resp = client.post("/update_job_status", json={
        "job_id": job_id,
        "status": "completed",
        "worker_id": "worker1",
        "runner_id": "runner1_old",
    })
    assert update_resp.status_code == 200

    # CRUCIAL BUG 10 ASSERTION : Le statut global du job DOIT RESTER 'running'
    # car le nœud 'train' est toujours en cours d'exécution !
    st_mid = client.get(f"/job_status/{job_id}").get_json()
    assert st_mid["status"] == "running", f"Le statut du job ne doit pas être 'completed', reçu: {st_mid['status']}"

    # Vérifier en DB directement
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
        assert cursor.fetchone()[0] == "running"

    # 6. Worker termine maintenant 'train'
    next3 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker_id": "worker1",
        "runner_id": "runner1",
        "node": "train",
        "status": "done",
        "duration_s": 5.0,
        "exit_code": 0,
    }).get_json()
    assert next3.get("action") == "finish"

    # 7. Worker termine sa tâche et poste /update_job_status completed
    final_update = client.post("/update_job_status", json={
        "job_id": job_id,
        "status": "completed",
        "worker_id": "worker1",
        "runner_id": "runner1",
    })
    assert final_update.status_code == 200

    # Maintenant que 100% des nœuds sont done, le job DOIT être 'completed'
    st_final = client.get(f"/job_status/{job_id}").get_json()
    assert st_final["status"] == "completed"

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
        assert cursor.fetchone()[0] == "completed"


def test_bug10_failed_node_with_running_sibling(client):
    """
    Vérifie qu'avec 2 branches indépendantes, l'échec de la première laisse le job
    'running' tant que la seconde est active, puis bascule à 'failed' à la fin.
    """
    client.post("/register_worker", json={
        "worker_id": "w1", "hostname": "w1", "cpus": 4, "total_ram_gb": 32.0, "available_storage_gb": 500.0, "role": "worker"
    })
    client.post("/register_worker", json={
        "worker_id": "w2", "hostname": "w2", "cpus": 4, "total_ram_gb": 32.0, "available_storage_gb": 500.0, "role": "worker"
    })

    plan = {
        "nodes": [
            {"name": "branch_a", "deps": [], "resources": {"cpus": 1, "ram_gb": 2.0}, "stale": True},
            {"name": "branch_b", "deps": [], "resources": {"cpus": 1, "ram_gb": 2.0}, "stale": True},
        ]
    }
    sub = client.post("/submit_job", json={
        "repo": "UNIL-DESI/cluster-ci", "branch": "main", "parallel_mode": 1, "plan": plan
    }).get_json()
    job_id = sub["job_id"]

    # Première passe pour assigner home_worker et auxiliary
    scheduler_loop.schedule_iteration()

    # w1 prend branch_a
    next_a = client.post(f"/api/jobs/{job_id}/next_node", json={"worker": "w1", "runner_id": "r1"}).get_json()
    assert next_a.get("action") in ("run", "switch_image")
    assert next_a.get("node") == "branch_a"

    # w2 prend branch_b
    next_b = client.post(f"/api/jobs/{job_id}/next_node", json={"worker": "w2", "runner_id": "r2"}).get_json()
    assert next_b["node"] == "branch_b"

    # branch_a échoue avec HostMemoryPressureExceeded (échec terminal sans retry)
    client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker_id": "w1",
        "runner_id": "r1",
        "node": "branch_a",
        "status": "failed",
        "failure_reason": "HostMemoryPressureExceeded",
        "exit_code": 137,
    })

    # branch_b tourne toujours -> le job doit rester 'running' !
    st = client.get(f"/job_status/{job_id}").get_json()
    assert st["status"] == "running"

    # branch_b se termine avec succès
    client.post(f"/api/jobs/{job_id}/next_node", json={
        "worker_id": "w2",
        "runner_id": "r2",
        "node": "branch_b",
        "status": "done",
        "exit_code": 0,
    })

    # Maintenant que plus rien ne tourne et qu'une branche a échoué -> le job est 'failed'
    st_end = client.get(f"/job_status/{job_id}").get_json()
    assert st_end["status"] == "failed"
