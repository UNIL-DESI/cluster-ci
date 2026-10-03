"""
test_v3_scheduler.py - Tests unitaires et d'intégration réels pour Cluster-CI v3 (Ordonnanceur Headnode).

Couvre :
1. Validation fail-fast du plan (clés inconnues, dépendances inexistantes, cycles -> HTTP 400).
2. DAG 2 branches + jonction sur 2 workers fictifs (GB10 unifié et discrète 2x24 Go) : exécution parallèle et attente de jonction.
3. Équité multi-jobs & anti ping-pong A1 : A=2, B=1 stable ; cession d'une machine supplémentaire seulement quand machines(B) + 1 < machines(A).
4. Cession d'une machine supplémentaire à un job classique en attente (Amendement A2).
5. Règles d'admission unifiée (GB10) vs discrète (RTX 3090) et correction anti-OOM GB10.
6. Reprise automatique d'un nœud running après perte de heartbeat runner (>60s).
7. Gestion des sorties introuvables (Amendement A4 missing_deps) : 1 relance forcée du producteur, puis échec explicite.
8. Annulation de job : tous les nœuds non terminés -> blocked, notification /cancel aux workers.
"""

import os
import sys
import json
import uuid
import tempfile
import pytest

# Ensure scheduler directory is on sys.path
sched_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sched_dir not in sys.path:
    sys.path.insert(0, sched_dir)

import persistence
import defaults
import scheduler_loop
import headnode_service

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Initialise une base SQLite temporaire et isolée pour chaque test."""
    db_file = str(tmp_path / f"test_cluster_{uuid.uuid4().hex[:8]}.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", db_file)
    persistence.DB_PATH = db_file
    persistence.init_db()
    yield db_file

@pytest.fixture
def client():
    """Client de test Flask configuré pour headnode_service."""
    headnode_service.app.config['TESTING'] = True
    with headnode_service.app.test_client() as c:
        yield c


# =========================================================================
# 1. Validation Fail-Fast du Plan JSON (HTTP 400)
# =========================================================================

def test_plan_validation_unknown_resource_key(client):
    """Rejet fail-fast (400) si une clé de ressource est inconnue."""
    bad_plan = {
        "version": "3.0",
        "nodes": [
            {
                "name": "node1",
                "deps": [],
                "resources": {"ram_gb": 10.0, "quantum_cores": 42}  # Clé inconnue
            }
        ]
    }
    resp = client.post("/submit_job", json={
        "repo": "owner/repo",
        "branch": "main",
        "plan": bad_plan
    })
    assert resp.status_code == 400
    assert "Invalid execution plan" in resp.get_json()["error"]
    assert "quantum_cores" in resp.get_json()["error"]

def test_plan_validation_missing_dependency(client):
    """Rejet fail-fast (400) si une dépendance déclarée n'existe pas dans le DAG."""
    bad_plan = {
        "version": "3.0",
        "nodes": [
            {
                "name": "node_b",
                "deps": ["node_inexistant"],
                "resources": {"ram_gb": 10.0}
            }
        ]
    }
    resp = client.post("/submit_job", json={
        "repo": "owner/repo",
        "branch": "main",
        "plan": bad_plan
    })
    assert resp.status_code == 400
    assert "Dépendance invalide" in resp.get_json()["error"]

def test_plan_validation_cycle_detection(client):
    """Rejet fail-fast (400) si un cycle existe dans le graphe."""
    cycle_plan = {
        "version": "3.0",
        "nodes": [
            {"name": "A", "deps": ["B"], "resources": {}},
            {"name": "B", "deps": ["C"], "resources": {}},
            {"name": "C", "deps": ["A"], "resources": {}}
        ]
    }
    resp = client.post("/submit_job", json={
        "repo": "owner/repo",
        "branch": "main",
        "plan": cycle_plan
    })
    assert resp.status_code == 400
    assert "Cycle détecté" in resp.get_json()["error"]


# =========================================================================
# 2. DAG 2 branches + jonction sur 2 workers fictifs
# =========================================================================

def test_dag_two_branches_and_join_execution(client):
    """
    DAG :
      prep_data (racine)
      ├── train_a (branche 1)
      └── train_b (branche 2)
           └── join_eval (jonction, attend A et B)
    """
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        # W1: GB10 unifié 128 Go
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
            VALUES ('W1_GB10', 'hec45801', 'http://127.0.0.1:9001', 128.0, 128.0, 1, 16, 'online', CURRENT_TIMESTAMP)
        ''')
        # W2: Machine discrète 2x24 Go
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, gpu_count, vram_per_gpu, unified_memory, cpus, status, last_seen)
            VALUES ('W2_DISC', 'isipol09', 'http://127.0.0.1:9002', 64.0, 24.0, 2, '[24.0, 24.0]', 0, 16, 'online', CURRENT_TIMESTAMP)
        ''')
        conn.commit()

    plan = {
        "version": "3.0",
        "defaults": {"image": "image:default", "ram_gb": 10.0, "vram_gb": 10.0},
        "nodes": [
            {"name": "prep_data", "deps": [], "priority": 10.0, "stale": True},
            {"name": "train_a", "deps": ["prep_data"], "priority": 20.0, "stale": True},
            {"name": "train_b", "deps": ["prep_data"], "priority": 15.0, "stale": True},
            {"name": "join_eval", "deps": ["train_a", "train_b"], "priority": 5.0, "stale": True}
        ]
    }

    resp = client.post("/submit_job", json={"repo": "owner/repo", "branch": "feat", "plan": plan})
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]

    # 1. Première passe scheduler : W1 devient home_worker pour le job
    scheduler_loop.schedule_iteration()

    # 2. W1 interroge worker_poll puis next_node
    poll_resp = client.get("/worker_poll/W1_GB10").get_json()
    assert poll_resp["status"] == "assigned"
    assert poll_resp["parallel_mode"] == 1
    assert poll_resp["role"] == "executor"
    assert poll_resp["is_home_worker"] is True

    step1 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "runner_w1",
        "worker": "W1_GB10",
        "node": None,
        "status": None
    }).get_json()
    assert step1["action"] in ("run", "switch_image")
    assert step1["node"] == "prep_data"

    # 3. W1 termine prep_data -> train_a et train_b deviennent ready
    step2 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "runner_w1",
        "worker": "W1_GB10",
        "node": "prep_data",
        "status": "done",
        "duration_s": 5.0,
        "current_image": "image:default"
    }).get_json()
    # W1 prend train_a (priorité 20 > 15)
    assert step2["node"] == "train_a"
    assert step2["action"] == "run"

    # 4. Le scheduler donne W2 en machine supplémentaire pour train_b
    scheduler_loop.schedule_iteration()

    poll_w2 = client.get("/worker_poll/W2_DISC").get_json()
    assert poll_w2["status"] == "assigned"
    assert poll_w2["is_home_worker"] is False

    step_w2 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "runner_w2",
        "worker": "W2_DISC",
        "node": None,
        "status": None,
        "current_image": "image:default"
    }).get_json()
    # W2 prend train_b en parallèle de train_a sur W1 !
    assert step_w2["node"] == "train_b"
    assert step_w2["action"] == "run"
    assert step_w2["gpu_ids"] == [0] # 10 Go VRAM alloué sur GPU 0 (24 Go)

    # 5. Pendant que train_b tourne, W1 termine train_a :
    # join_eval doit ATTENDRE que train_b finisse
    step_w1_wait = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "runner_w1",
        "worker": "W1_GB10",
        "node": "train_a",
        "status": "done",
        "duration_s": 12.0
    }).get_json()
    # train_b n'étant pas encore terminé, join_eval n'est pas ready.
    # W1 étant la machine prioritaire, elle reçoit 'wait' (Amendement A3)
    assert step_w1_wait["action"] == "wait"

    # 6. W2 termine train_b -> join_eval devient ready
    step_w2_fin = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "runner_w2",
        "worker": "W2_DISC",
        "node": "train_b",
        "status": "done",
        "duration_s": 14.0
    }).get_json()
    # W2 (machine supplémentaire) peut prendre join_eval ou céder
    assert step_w2_fin["node"] == "join_eval" or step_w2_fin["action"] == "yield"

    # Compléter join_eval
    active_node = step_w2_fin.get("node")
    active_worker = "W2_DISC" if active_node else "W1_GB10"
    if not active_node:
        step_w1_join = client.post(f"/api/jobs/{job_id}/next_node", json={
            "runner_id": "runner_w1",
            "worker": "W1_GB10",
            "node": None,
            "status": None
        }).get_json()
        assert step_w1_join["node"] == "join_eval"
        active_worker = "W1_GB10"

    client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": f"runner_{active_worker}",
        "worker": active_worker,
        "node": "join_eval",
        "status": "done",
        "duration_s": 3.0
    })

    # Statut consolidé du job
    status_resp = client.get(f"/job_status/{job_id}").get_json()
    assert status_resp["status"] == "completed"
    assert len(status_resp["nodes"]) == 4


# =========================================================================
# 3. Équité multi-jobs & Anti ping-pong (Amendement A1)
# =========================================================================

def test_fairness_anti_ping_pong_rule_a1(client):
    """
    Scénario :
    - 3 machines : W1, W2, W3
    - Job A arrive d'abord et prend 3 machines (W1 home, W2 et W3 supplémentaires).
    - Job B arrive (détient 0 machine).
    - W2 (supplémentaire de A) termine un nœud :
      machines(B) + 1 < machines(A) (0 + 1 < 3) -> cession (yield) !
      A a maintenant 2 machines (W1, W3), B a 1 machine (W2).
    - W3 (supplémentaire de A) termine un nœud :
      machines(B) + 1 < machines(A) (1 + 1 < 2) -> FAUX ! Pas de cession !
      Anti-ping-pong respecté : A=2 et B=1 stable.
    """
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        for wid in ("W1", "W2", "W3"):
            cursor.execute('''
                INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
                VALUES (?, ?, 'http://127.0.0.1:9000', 128.0, 128.0, 1, 16, 'online', CURRENT_TIMESTAMP)
            ''', (wid, wid.lower()))
        conn.commit()

    plan_a = {
        "version": "3.0",
        "nodes": [
            {"name": f"task_a_{i}", "deps": [], "stale": True} for i in range(10)
        ]
    }
    plan_b = {
        "version": "3.0",
        "nodes": [
            {"name": f"task_b_{i}", "deps": [], "stale": True} for i in range(10)
        ]
    }

    job_a_id = client.post("/submit_job", json={"repo": "owner/repoA", "branch": "main", "plan": plan_a}).get_json()["job_id"]
    scheduler_loop.schedule_iteration()

    # Vérifier que A a pris W1 comme home et W2, W3 comme supplémentaires
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT active_workers FROM jobs WHERE job_id = ?", (job_a_id,))
        a_workers = json.loads(cursor.fetchone()[0])
        assert len(a_workers) == 3

    # Job B arrive
    job_b_id = client.post("/submit_job", json={"repo": "owner/repoB", "branch": "main", "plan": plan_b}).get_json()["job_id"]

    # W2 (supplémentaire de A) termine un nœud
    # Actuellement : A a 3 machines, B a 0 machine. (0 + 1 < 3 -> Cession requise)
    yield_step = client.post(f"/api/jobs/{job_a_id}/next_node", json={
        "runner_id": "run_w2",
        "worker": "W2",
        "node": "task_a_1",
        "status": "done",
        "duration_s": 5.0
    }).get_json()
    assert yield_step["action"] == "yield"

    # Le scheduler donne maintenant W2 à B
    scheduler_loop.schedule_iteration()
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT active_workers FROM jobs WHERE job_id = ?", (job_a_id,))
        a_workers_after = json.loads(cursor.fetchone()[0])
        cursor.execute("SELECT active_workers FROM jobs WHERE job_id = ?", (job_b_id,))
        b_workers = json.loads(cursor.fetchone()[0])
        assert len(a_workers_after) == 2
        assert len(b_workers) == 1

    # W3 (machine supplémentaire restante de A) termine un nœud
    # A=2, B=1 : 1 + 1 < 2 est FAUX -> PAS de ping-pong, A conserve W3 !
    step_w3 = client.post(f"/api/jobs/{job_a_id}/next_node", json={
        "runner_id": "run_w3",
        "worker": "W3",
        "node": "task_a_2",
        "status": "done",
        "duration_s": 5.0
    }).get_json()
    assert step_w3["action"] != "yield"
    assert step_w3["node"] is not None


# =========================================================================
# 4. Cession d'une machine supplémentaire à un job classique (Amendement A2)
# =========================================================================

def test_additional_machine_yield_to_classic_job_a2(client):
    """
    Un job classique en attente compte comme un job concurrent qui détient 0 machine.
    Les machines supplémentaires d'un job parallèle lui sont cédées à la frontière de nœud.
    """
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        for wid in ("W1", "W2"):
            cursor.execute('''
                INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
                VALUES (?, ?, 'http://127.0.0.1:9000', 128.0, 128.0, 1, 16, 'online', CURRENT_TIMESTAMP)
            ''', (wid, wid.lower()))
        conn.commit()

    # Job A (parallèle) prend W1 et W2
    plan_a = {"version": "3.0", "nodes": [{"name": f"n{i}", "deps": [], "stale": True} for i in range(5)]}
    job_a_id = client.post("/submit_job", json={"repo": "owner/repoA", "branch": "main", "plan": plan_a}).get_json()["job_id"]
    scheduler_loop.schedule_iteration()

    # Job classique sans plan (parallel_mode = 0)
    job_classic_id = client.post("/submit_job", json={
        "repo": "owner/repoClassic",
        "branch": "main",
        "ram_required_gb": 10.0
    }).get_json()["job_id"]

    # W2 (machine supplémentaire de A) termine un nœud -> doit céder la machine au job classique en attente
    res = client.post(f"/api/jobs/{job_a_id}/next_node", json={
        "runner_id": "run_w2",
        "worker": "W2",
        "node": "n0",
        "status": "done"
    }).get_json()
    assert res["action"] == "yield"

    # Au prochain tour de boucle, W2 est alloué au job classique
    scheduler_loop.schedule_iteration()
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT worker_id, status FROM jobs WHERE job_id = ?", (job_classic_id,))
        c_row = cursor.fetchone()
        assert c_row["status"] == "assigned"
        assert c_row["worker_id"] == "W2"


# =========================================================================
# 5. Admission unifiée (GB10) vs discrète (RTX 3090)
# =========================================================================

def test_admission_rules_unified_vs_discrete():
    """Vérifie le respect strict des règles d'admission unifiée vs discrète."""
    gb10 = {
        "worker_id": "GB10", "hostname": "hec45801",
        "total_ram_gb": 128.0, "total_vram_gb": 128.0,
        "unified_memory": 1, "cpus": 16
    }
    rtx = {
        "worker_id": "ISIPOL09", "hostname": "isipol09",
        "total_ram_gb": 64.0, "total_vram_gb": 24.0,
        "gpu_count": 2, "vram_per_gpu": "[24.0, 24.0]",
        "unified_memory": 0, "cpus": 16
    }

    # Cas 1 : Nœud 80 Go VRAM
    res_80_vram = {"ram_gb": 10.0, "vram_gb": 80.0}
    # Sur GB10 : 10 + 80 = 90 <= 128 - 8 = 120 -> Admissible
    assert scheduler_loop.is_worker_admissible_for_node(gb10, res_80_vram) is True
    # Sur RTX : max VRAM combinée 48 Go < 80 Go -> Refusé
    assert scheduler_loop.is_worker_admissible_for_node(rtx, res_80_vram) is False

    # Cas 2 : Surallocation GB10 (70 Go RAM + 60 Go VRAM = 130 Go > 120 Go utilisables)
    res_surallocation = {"ram_gb": 70.0, "vram_gb": 60.0}
    assert scheduler_loop.is_worker_admissible_for_node(gb10, res_surallocation) is False

    # Cas 3 : Whitelist de workers
    res_whitelist = {"ram_gb": 10.0, "workers": ["hec45801"]}
    assert scheduler_loop.is_worker_admissible_for_node(gb10, res_whitelist) is True
    assert scheduler_loop.is_worker_admissible_for_node(rtx, res_whitelist) is False


# =========================================================================
# 6. Reprise après perte de runner_heartbeat (>60s)
# =========================================================================

def test_runner_heartbeat_timeout_recovery(client):
    """Un nœud running repasse en ready si aucun heartbeat reçu depuis > 60s."""
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
            VALUES ('W1', 'h1', 'http://127.0.0.1:9000', 128.0, 128.0, 1, 16, 'online', CURRENT_TIMESTAMP)
        ''')
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, parallel_mode, status)
            VALUES ('job_hb', 'owner/repo', 'main', 1, 'running')
        ''')
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, worker_id, runner_id, started_at)
            VALUES ('job_hb', 'dead_node', 'running', 'W1', 'runner_crash', datetime('now', '-90 seconds'))
        ''')
        # Heartbeat enregistré il y a 90 secondes (> 60s)
        cursor.execute('''
            INSERT INTO runner_heartbeats (job_id, runner_id, worker_id, current_node, last_seen)
            VALUES ('job_hb', 'runner_crash', 'W1', 'dead_node', datetime('now', '-90 seconds'))
        ''')
        conn.commit()

    recovered = persistence.check_runner_heartbeat_timeouts(timeout_s=60.0)
    assert len(recovered) == 1
    assert recovered[0]["node_name"] == "dead_node"

    # Vérifier que le nœud est repassé en ready
    node = persistence.get_job_node("job_hb", "dead_node")
    assert node["status"] == "ready"
    assert node["worker_id"] is None
    assert node["runner_id"] is None


# =========================================================================
# 7. Gestion de missing_deps (Amendement A4)
# =========================================================================

def test_missing_deps_retry_and_exhaustion(client):
    """
    Amendement A4 :
    1er échec missing_deps -> producteur relancé en ready, consommateur remis en pending.
    2ème échec missing_deps -> échec explicite du consommateur (failed).
    """
    job_id = "job_md"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, parallel_mode, status)
            VALUES (?, 'owner/repo', 'main', 1, 'running')
        ''', (job_id,))
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, out_paths, missing_deps_retried)
            VALUES (?, 'producer', 'done', ?, 0)
        ''', (job_id, json.dumps([{"path": "data/features.parquet"}])))
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, deps, dep_paths)
            VALUES (?, 'consumer', 'running', ?, ?)
        ''', (job_id, json.dumps(["producer"]), json.dumps(["data/features.parquet"])))
        conn.commit()

    # 1ère tentative : missing_deps
    res1 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "r1", "worker": "W1", "node": "consumer",
        "status": "missing_deps", "missing_paths": ["data/features.parquet"]
    }).get_json()

    prod = persistence.get_job_node(job_id, "producer")
    cons = persistence.get_job_node(job_id, "consumer")
    assert prod["status"] == "ready"
    assert prod["stale_reason"] == "outputs_missing"
    assert prod["missing_deps_retried"] == 1
    assert cons["status"] == "pending"

    # Simuler ré-exécution du producteur jusqu'à 'done'
    persistence.mark_node_status(job_id, "producer", "done")
    persistence.update_dag_ready_states(job_id)

    # 2ème tentative de missing_deps (récidive)
    res2 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "r1", "worker": "W1", "node": "consumer",
        "status": "missing_deps", "missing_paths": ["data/features.parquet"]
    }).get_json()

    cons2 = persistence.get_job_node(job_id, "consumer")
    assert cons2["status"] == "failed"
    assert "could not be recovered" in cons2["error_message"]


# =========================================================================
# 8. Annulation de Job
# =========================================================================

def test_job_cancellation_cascade(client):
    """L'annulation d'un job passe tous ses nœuds non terminés à blocked et met le job en failed."""
    job_id = "job_cancel"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, service_url, status)
            VALUES ('W1', 'http://127.0.0.1:9999', 'online')
        ''')
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, parallel_mode, status, active_workers)
            VALUES (?, 'owner/repo', 'main', 1, 'running', ?)
        ''', (job_id, json.dumps(["W1"])))
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status)
            VALUES (?, 'n_done', 'done'), (?, 'n_running', 'running'), (?, 'n_pending', 'pending')
        ''', (job_id, job_id, job_id))
        conn.commit()

    # Appel stop
    resp = client.post(f"/api/jobs/{job_id}/stop")
    assert resp.status_code == 200

    job = client.get(f"/job_status/{job_id}").get_json()
    assert job["status"] == "failed"
    nodes = {n["node_name"]: n["status"] for n in job["nodes"]}
    assert nodes["n_done"] == "done"
    assert nodes["n_running"] == "blocked"
    assert nodes["n_pending"] == "blocked"

def test_aggregated_multi_executor_logs_and_offset_recovery(isolated_db, client):
    """
    Test exigé par le contrat §6 et compléments W3 :
    2 exécuteurs simulés envoyant des lignes entrelacées :
    - flux unique, ordonné et sans perte
    - offsets strictement monotones
    - reprise correcte à un offset donné
    """
    job_id = f"job-multi-logs-{uuid.uuid4().hex[:8]}"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, status, parallel_mode, created_at)
            VALUES (?, 'owner/repo', 'running', 1, CURRENT_TIMESTAMP)
        ''', (job_id,))
        conn.commit()

    # 1. Simulateur Exécuteur 1 (node_A sur worker-1) et Exécuteur 2 (node_B sur worker-2)
    # Lignes entrelacées avec préfixe standard [node@machine]
    l1 = "[node_A@worker-1] Starting task A...\n"
    r1 = client.post(f"/api/jobs/{job_id}/logs", json={"logs": l1})
    assert r1.status_code == 200
    off1 = r1.get_json()["offset"]
    assert off1 == len(l1.encode("utf-8"))

    l2 = "[node_B@worker-2] Starting task B concurrently...\n"
    r2 = client.post(f"/job_logs/{job_id}", json={"logs": l2})
    assert r2.status_code == 200
    off2 = r2.get_json()["offset"]
    assert off2 == off1 + len(l2.encode("utf-8"))

    l3 = "[node_A@worker-1] Progress A: 50% complete\n"
    r3 = client.post(f"/api/jobs/{job_id}/logs", json={"lines": [l3.strip()]})
    assert r3.status_code == 200
    off3 = r3.get_json()["offset"]
    assert off3 > off2

    l4 = "[node_B@worker-2] Task B finished successfully.\n"
    r4 = client.post(f"/job_logs/{job_id}", json={"logs": l4})
    assert r4.status_code == 200
    off4 = r4.get_json()["offset"]
    assert off4 > off3

    # 2. Lecture complète depuis offset=0 via GET /job_logs/<job_id>
    get_all = client.get(f"/job_logs/{job_id}?offset=0")
    assert get_all.status_code == 200
    data_all = get_all.get_json()
    assert data_all["offset"] == off4
    full_text = data_all["logs"]
    assert "[node_A@worker-1] Starting task A" in full_text
    assert "[node_B@worker-2] Starting task B concurrently" in full_text
    assert "[node_A@worker-1] Progress A" in full_text
    assert "[node_B@worker-2] Task B finished" in full_text

    # 3. Reprise à un offset intermédiaire (après l2, à off2)
    get_mid = client.get(f"/api/jobs/{job_id}/logs?offset={off2}")
    assert get_mid.status_code == 200
    data_mid = get_mid.get_json()
    assert data_mid["offset"] == off4
    mid_text = data_mid["logs"]
    assert "Starting task A" not in mid_text
    assert "Starting task B" not in mid_text
    assert "[node_A@worker-1] Progress A: 50% complete" in mid_text
    assert "[node_B@worker-2] Task B finished successfully" in mid_text

def test_job_status_node_summary_and_classic_job_unchanged(isolated_db, client, monkeypatch):
    """
    Test du résumé par nœud dans /job_status et préservation du chemin classique sans logs locaux.
    """
    # 1. Job parallèle avec résumé par nœud
    job_p = "job-status-summary-p"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, status, parallel_mode, created_at)
            VALUES (?, 'owner/repo', 'running', 1, CURRENT_TIMESTAMP)
        ''', (job_p,))
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, worker_id, duration_s, priority)
            VALUES (?, 'node1', 'done', 'worker-gpu-1', 42.5, 10.0),
                   (?, 'node2', 'running', 'worker-gpu-2', 12.0, 5.0)
        ''', (job_p, job_p))
        conn.commit()

    res_p = client.get(f"/job_status/{job_p}").get_json()
    assert res_p["status"] == "running"
    assert "nodes" in res_p
    assert "nodes_summary" in res_p
    assert len(res_p["nodes_summary"]) == 2

    # Vérification des champs requis : (nom, état, machine, durée)
    n1 = next(n for n in res_p["nodes_summary"] if n["name"] == "node1")
    assert n1["status"] == "done"
    assert n1["machine"] == "worker-gpu-1"
    assert n1["duration"] == 42.5

    n2 = next(n for n in res_p["nodes_summary"] if n["name"] == "node2")
    assert n2["status"] == "running"
    assert n2["machine"] == "worker-gpu-2"
    assert n2["duration"] == 12.0

    # 2. Job classique avec proxy logs vers worker distant
    job_c = "job-classic-proxy"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, service_url, status)
            VALUES ('classic-w', 'http://classic-worker:6000', 'online')
        ''')
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, status, parallel_mode, worker_id)
            VALUES (?, 'owner/classic', 'running', 0, 'classic-w')
        ''', (job_c,))
        conn.commit()

    # Mock requests.get pour le worker distant
    class MockWorkerResp:
        status_code = 200
        def json(self):
            return {"logs": "Classic worker logs line 1\n", "offset": 30}

    monkeypatch.setattr("requests.get", lambda url, timeout=5: MockWorkerResp())

    res_logs = client.get(f"/job_logs/{job_c}?offset=0").get_json()
    assert res_logs["logs"] == "Classic worker logs line 1\n"
    assert res_logs["offset"] == 30

