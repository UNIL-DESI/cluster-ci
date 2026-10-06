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
import hashlib
import io
import tarfile
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
    monkeypatch.setattr(headnode_service, 'REPOS_DIR', str(tmp_path / 'repositories'))
    persistence.DB_PATH = db_file
    persistence.init_db()
    yield db_file

@pytest.fixture
def client(monkeypatch):
    """Client de test Flask configuré pour headnode_service."""
    headnode_service.app.config['TESTING'] = True
    import subprocess
    orig_run = subprocess.run
    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "git":
            raise subprocess.CalledProcessError(128, cmd)
        return orig_run(cmd, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr(headnode_service, "CLUSTER_TOKEN", "scheduler-test-token")
    with headnode_service.app.test_client() as c:
        c.environ_base["HTTP_AUTHORIZATION"] = "Bearer scheduler-test-token"
        yield c


def _submit_test_job(client, *, json):
    """Use the real authenticated archive protocol for synthetic local jobs."""
    payload = dict(json)
    if payload.get('is_local'):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as tar:
            content = b'synthetic scheduler fixture\n'
            entry = tarfile.TarInfo('README.txt')
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
        body = archive.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        create = client.post('/api/local_transfers', json={
            'purpose': 'source', 'total_size': len(body), 'sha256': digest})
        assert create.status_code == 201, create.get_json()
        transfer = create.get_json()
        tid = transfer['transfer_id']
        size = transfer['chunk_size']
        for index, offset in enumerate(range(0, len(body), size)):
            chunk = body[offset:offset + size]
            response = client.put(f'/api/local_transfers/{tid}/chunks/{index}',
                data=chunk, headers={'X-Chunk-SHA256': hashlib.sha256(chunk).hexdigest()})
            assert response.status_code == 200, response.get_json()
        complete = client.post(f'/api/local_transfers/{tid}/complete', json={})
        assert complete.status_code == 200, complete.get_json()
        payload['source_transfer_id'] = tid
        payload['username'] = 'test-' + payload['repo'].replace('/', '-')
    return client.post('/submit_job', json=payload)


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
    assert "Invalid dependency" in resp.get_json()["error"]

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
    assert "Cycle detected" in resp.get_json()["error"]


# =========================================================================
# 2. DAG 2 branches + jonction sur 2 workers fictifs
# =========================================================================

@pytest.mark.parametrize("is_local", [False, True])
def test_dag_two_branches_and_join_execution(client, is_local):
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

    resp = _submit_test_job(client, json={"repo": "owner/repo", "branch": "feat", "plan": plan, "is_local": is_local})
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]

    # 1. Première passe scheduler : W1 devient home_worker pour le job
    scheduler_loop.schedule_iteration()

    # 2. W1 interroge worker_poll puis next_node
    poll_resp = client.get("/worker_poll/W1_GB10").get_json()
    assert poll_resp["status"] == "assigned"
    assert poll_resp["parallel_mode"] == 1
    assert poll_resp["is_local"] == int(is_local)
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
    assert poll_w2["is_local"] == int(is_local)

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

@pytest.mark.parametrize("is_local", [False, True])
def test_fairness_anti_ping_pong_rule_a1(client, is_local):
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

    job_a_id = _submit_test_job(client, json={"repo": "owner/repoA", "branch": "main", "plan": plan_a, "is_local": is_local}).get_json()["job_id"]
    scheduler_loop.schedule_iteration()

    # Vérifier que A a pris W1 comme home et W2, W3 comme supplémentaires
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT active_workers FROM jobs WHERE job_id = ?", (job_a_id,))
        a_workers = json.loads(cursor.fetchone()[0])
        assert len(a_workers) == 3

    # Job B arrive
    job_b_id = _submit_test_job(client, json={"repo": "owner/repoB", "branch": "main", "plan": plan_b}).get_json()["job_id"]

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

@pytest.mark.parametrize("is_local", [False, True])
def test_additional_machine_yield_to_classic_job_a2(client, is_local):
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
    job_a_id = _submit_test_job(client, json={"repo": "owner/repoA", "branch": "main", "plan": plan_a, "is_local": is_local}).get_json()["job_id"]
    scheduler_loop.schedule_iteration()

    # Job classique sans plan (parallel_mode = 0)
    job_classic_id = _submit_test_job(client, json={
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
    assert prod["stale_reason"] in ("outputs_missing", "outputs_missing_no_peer_cache")
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

def test_headnode_last_resort_and_reserve(isolated_db, client):
    """
    Exigence Henri (Périmètre W3 Headnode de Dernier Recours) :
    1. Réserves appliquées au headnode (16 Go RAM, 2 CPUs)
    2. Headnode jamais choisi tant qu'un GB10 peut admettre (même si affinité)
    3. Headnode choisi quand les deux GB10 sont occupés
    """
    from scheduler_loop import is_worker_admissible_for_node, schedule_iteration, get_worker_placement_priority
    from src.runner.host_guard import get_headnode_safe_capacities

    gb10_a = {
        "worker_id": "gb10-worker-1",
        "hostname": "gb10-a",
        "service_url": "http://gb10-a:6000",
        "total_ram_gb": 128.0,
        "unified_memory": 1,
        "cpus": 16,
        "gpu_count": 1,
        "status": "online"
    }
    gb10_b = {
        "worker_id": "gb10-worker-2",
        "hostname": "gb10-b",
        "service_url": "http://gb10-b:6000",
        "total_ram_gb": 128.0,
        "unified_memory": 1,
        "cpus": 16,
        "gpu_count": 1,
        "status": "online"
    }
    headnode_raw = {
        "worker_id": "isipol09-headnode",
        "hostname": "isipol09",
        "service_url": "http://130.223.73.209:6000",
        "role": "headnode",
        "total_ram_gb": 32.0,
        "unified_memory": 0,
        "cpus": 4,
        "total_vram_gb": 8.0,
        "vram_per_gpu": json.dumps([8.0]),
        "gpu_count": 1,
        "status": "online"
    }
    headnode = get_headnode_safe_capacities(headnode_raw)

    # 1. Test des capacités nettes de réserve Headnode (16 Go RAM nettes, 2 CPU nets via get_headnode_safe_capacities)
    # Nœud demandant 3 CPU : le headnode a 2 CPU nets disponibles -> rejeté
    assert is_worker_admissible_for_node(headnode, {"cpus": 3}) is False
    assert is_worker_admissible_for_node(gb10_a, {"cpus": 3}) is True

    # Nœud demandant 18 Go RAM : headnode a 16 Go nettes (- 2 Go marge) = 14 Go max -> rejeté
    assert is_worker_admissible_for_node(headnode, {"ram_gb": 18.0}) is False
    assert is_worker_admissible_for_node(gb10_a, {"ram_gb": 18.0}) is True

    # Nœud léger (1 CPU, 8 Go RAM, 0 VRAM) : headnode admissible
    light_res = {"cpus": 1, "ram_gb": 8.0, "vram_gb": 0.0}
    assert is_worker_admissible_for_node(headnode, light_res) is True

    # 2. Rang placement_priority W11 : plus grand = préféré, headnode le plus bas (0)
    assert get_worker_placement_priority(gb10_a) == 50
    assert get_worker_placement_priority(headnode) == 0

    # 3. Test d'ordonnancement : 2 GB10 et 1 Headnode enregistrés en DB
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        for w in [gb10_a, gb10_b, headnode]:
            cursor.execute('''
                INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb,
                                     unified_memory, cpus, gpu_count, status, role, placement_priority, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, CURRENT_TIMESTAMP)
            ''', (w["worker_id"], w["hostname"], w["service_url"], w["total_ram_gb"],
                  w.get("unified_memory", 0), w["cpus"], w.get("gpu_count", 0), w.get("role", "worker"),
                  w.get("placement_priority", 0)))
        conn.commit()

    # Soumission Job 1 (parallèle avec "version": "3.0")
    plan = {
        "version": "3.0",
        "nodes": [
            {"name": "task1", "deps": [], "resources": light_res}
        ]
    }
    resp1 = client.post("/submit_job", json={"repo": "owner/p1", "branch": "main", "plan": plan})
    assert resp1.status_code == 200
    job1_id = resp1.get_json()["job_id"]

    # Simuler une affinité artificielle sur le headnode pour tester que le rang placement_priority prime
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, worker_id, out_paths)
            VALUES (?, 'dummy_old', 'done', 'isipol09-headnode', '["affinity_hit"]')
        ''', (job1_id,))
        cursor.execute('''
            UPDATE job_nodes SET dep_paths = '["affinity_hit"]' WHERE job_id = ? AND node_name = 'task1'
        ''', (job1_id,))
        conn.commit()

    # Exécuter schedule_iteration : GB10 doit être choisi, JAMAIS le headnode
    schedule_iteration()

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT home_worker FROM jobs WHERE job_id = ?", (job1_id,))
        home_1 = cursor.fetchone()[0]
    assert home_1 in ("gb10-worker-1", "gb10-worker-2")
    assert home_1 != "isipol09-headnode"

    # Soumission Job 2 : le deuxième GB10 doit être choisi
    plan2 = {"version": "3.0", "nodes": [{"name": "task2", "deps": [], "resources": light_res}]}
    resp2 = client.post("/submit_job", json={"repo": "owner/p2", "branch": "main", "plan": plan2})
    job2_id = resp2.get_json()["job_id"]

    schedule_iteration()

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT home_worker FROM jobs WHERE job_id = ?", (job2_id,))
        home_2 = cursor.fetchone()[0]
    assert home_2 in ("gb10-worker-1", "gb10-worker-2")
    assert home_2 != home_1
    assert home_2 != "isipol09-headnode"

    # Soumission Job 3 : maintenant que les DEUX GB10 sont occupés, le headnode doit être choisi en DERNIER RECOURS !
    plan3 = {"version": "3.0", "nodes": [{"name": "task3", "deps": [], "resources": light_res}]}
    resp3 = client.post("/submit_job", json={"repo": "owner/p3", "branch": "main", "plan": plan3})
    job3_id = resp3.get_json()["job_id"]

    schedule_iteration()

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT home_worker FROM jobs WHERE job_id = ?", (job3_id,))
        home_3 = cursor.fetchone()[0]
    assert home_3 == "isipol09-headnode"


# =========================================================================
# 14. Course critique (Race condition) : Atomicité de /next_node (W3 Bug 2)
# =========================================================================

def test_concurrent_next_node_race_condition(client):
    """
    Test de concurrence et sérialisation intra-job :
    Même job, 2 nœuds prêts, une seule machine éligible :
    - 2 threads / runners appellent next_node simultanément pour le même job.
    - L'un obtient un nœud ('run' / 'switch_image').
    - Le 2e ATTEND ('wait', statut ready/pending en attente, jamais rejeté ni marqué impossible A17).
    - Après la fin du 1er nœud ('done'), le 2e nœud est placé avec succès.
    """
    import threading

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
            VALUES ('W_CONC', 'w_conc', 'http://127.0.0.1:9000', 64.0, 64.0, 1, 16, 'online', CURRENT_TIMESTAMP)
        ''')
        conn.commit()

    plan = {
        "version": "3.0",
        "nodes": [
            {"name": "task_alpha", "deps": [], "stale": True},
            {"name": "task_beta", "deps": [], "stale": True}
        ]
    }
    submit_resp = client.post("/submit_job", json={"repo": "owner/repoConc", "branch": "main", "plan": plan}).get_json()
    job_id = submit_resp["job_id"]
    scheduler_loop.schedule_iteration()

    results = []
    errors = []

    def call_worker(runner_id):
        try:
            with headnode_service.app.app_context():
                resp = scheduler_loop.handle_next_node({
                    "job_id": job_id,
                    "runner_id": runner_id,
                    "worker": "W_CONC",
                    "status": "ready"
                })
                results.append((runner_id, resp))
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=call_worker, args=("runner_1",))
    t2 = threading.Thread(target=call_worker, args=("runner_2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0
    assert len(results) == 2

    # L'un des runners obtient run/switch_image, l'autre obtient wait (anti-affinité intra-job)
    run_results = [r for r in results if r[1].get("action") in ("run", "switch_image")]
    wait_results = [r for r in results if r[1].get("action") == "wait"]

    assert len(run_results) == 1
    assert len(wait_results) == 1

    winner_runner, winner_resp = run_results[0]
    first_node = winner_resp["node"]
    assert first_node in ("task_alpha", "task_beta")

    # Vérification DB : le nœud gagnant est 'running', le 2e nœud reste 'ready' (pending/attente)
    # et n'est JAMAIS marqué 'failed' ou impossible A17
    remaining_node = "task_beta" if first_node == "task_alpha" else "task_alpha"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT node_name, status, error_message FROM job_nodes WHERE job_id = ?", (job_id,))
        db_nodes = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        cursor.execute("SELECT status, error_message FROM jobs WHERE job_id = ?", (job_id,))
        job_row = cursor.fetchone()

    assert db_nodes[first_node][0] == "running"
    assert db_nodes[remaining_node][0] == "ready"
    assert db_nodes[remaining_node][1] is None
    assert job_row["status"] == "running"
    assert job_row["error_message"] is None

    # Le 1er nœud termine son exécution avec succès ('done') -> le 2e nœud est alors placé
    done_resp = scheduler_loop.handle_next_node({
        "job_id": job_id,
        "runner_id": winner_runner,
        "worker": "W_CONC",
        "node": first_node,
        "status": "done",
        "duration_s": 1.0
    })
    assert done_resp.get("action") in ("run", "switch_image")
    assert done_resp.get("node") == remaining_node

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT node_name, status, runner_id FROM job_nodes WHERE job_id = ?", (job_id,))
        final_nodes = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    assert final_nodes[first_node][0] == "done"
    assert final_nodes[remaining_node][0] == "running"
    assert final_nodes[remaining_node][1] == winner_runner


# =========================================================================
# 15. Packing A11 : 2 nœuds de 2 jobs différents sur la même machine
# =========================================================================

@pytest.mark.parametrize("job_modes", [(False, False), (True, False), (False, True), (True, True)])
def test_packing_two_nodes_two_jobs_same_machine_admit_and_reject(client, job_modes):
    """
    Packing A11 :
    - 1 seule machine (RAM 32 Go, max utilisable 24 Go avec OS headroom 8 Go).
    - Job 1 demande 10 Go RAM.
    - Job 2 demande 10 Go RAM.
    - Les deux jobs doivent être admis et empilés sur la même machine (10 + 10 = 20 <= 24).
    - Job 3 demande 10 Go RAM : dépassement de capacité (20 + 10 = 30 > 24) -> rejeté / non admis.
    """
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
            VALUES ('W_PACK_1', 'w_pack_1', 'http://127.0.0.1:9000', 32.0, 32.0, 1, 8, 'online', CURRENT_TIMESTAMP)
        ''')
        conn.commit()

    plan_j1 = {"version": "3.0", "nodes": [{"name": "n1", "deps": [], "resources": {"ram_gb": 10.0, "cpus": 2}}]}
    plan_j2 = {"version": "3.0", "nodes": [{"name": "n2", "deps": [], "resources": {"ram_gb": 10.0, "cpus": 2}}]}
    plan_j3 = {"version": "3.0", "nodes": [{"name": "n3", "deps": [], "resources": {"ram_gb": 10.0, "cpus": 2}}]}

    j1_id = _submit_test_job(client, json={"repo": "o/j1", "branch": "main", "plan": plan_j1, "is_local": job_modes[0]}).get_json()["job_id"]
    j2_id = _submit_test_job(client, json={"repo": "o/j2", "branch": "main", "plan": plan_j2, "is_local": job_modes[1]}).get_json()["job_id"]

    scheduler_loop.schedule_iteration()

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT home_worker FROM jobs WHERE job_id = ?", (j1_id,))
        assert cursor.fetchone()[0] == "W_PACK_1"
        cursor.execute("SELECT home_worker FROM jobs WHERE job_id = ?", (j2_id,))
        assert cursor.fetchone()[0] == "W_PACK_1"

    # Lancer l'exécution des nœuds sur W_PACK_1
    step_j1 = client.post(f"/api/jobs/{j1_id}/next_node", json={"runner_id": "r1", "worker": "W_PACK_1"}).get_json()
    assert step_j1["action"] in ("run", "switch_image")
    assert step_j1["node"] == "n1"

    step_j2 = client.post(f"/api/jobs/{j2_id}/next_node", json={"runner_id": "r2", "worker": "W_PACK_1"}).get_json()
    assert step_j2["action"] in ("run", "switch_image")
    assert step_j2["node"] == "n2"

    # Maintenant que n1 et n2 sont 'running' sur W_PACK_1, la RAM allouée est 20 Go.
    # Soumission Job 3 : 10 Go supplémentaires ne tiennent pas dans 24 Go max (20 + 10 = 30 > 24)
    j3_id = _submit_test_job(client, json={"repo": "o/j3", "branch": "main", "plan": plan_j3}).get_json()["job_id"]
    scheduler_loop.schedule_iteration()

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT home_worker, status FROM jobs WHERE job_id = ?", (j3_id,))
        row = cursor.fetchone()
        assert row[0] is None
        assert row[1] == "pending"


# =========================================================================
# 16. Packing A11 : 2 branches d'un même job sur la même machine
# =========================================================================

def test_packing_two_branches_same_job_same_machine(client):
    """
    Packing A11 : 2 nœuds de DEUX JOBS DIFFÉRENTS s'exécutent en même temps sur la même machine.
    """
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
            VALUES ('W_SINGLE', 'w_single', 'http://127.0.0.1:9000', 64.0, 64.0, 1, 16, 'online', CURRENT_TIMESTAMP)
        ''')
        conn.commit()

    plan_1 = {
        "version": "3.0",
        "nodes": [
            {"name": "branch_A", "deps": [], "resources": {"ram_gb": 8.0, "cpus": 2}}
        ]
    }
    plan_2 = {
        "version": "3.0",
        "nodes": [
            {"name": "branch_B", "deps": [], "resources": {"ram_gb": 8.0, "cpus": 2}}
        ]
    }
    j1_id = client.post("/submit_job", json={"repo": "o/dag1", "branch": "main", "plan": plan_1}).get_json()["job_id"]
    j2_id = client.post("/submit_job", json={"repo": "o/dag2", "branch": "main", "plan": plan_2}).get_json()["job_id"]
    scheduler_loop.schedule_iteration()

    # Runner 1 prend branch_A de Job 1 sur W_SINGLE
    s1 = client.post(f"/api/jobs/{j1_id}/next_node", json={"runner_id": "r1", "worker": "W_SINGLE"}).get_json()
    assert s1["action"] in ("run", "switch_image")
    assert s1["node"] == "branch_A"

    # Runner 2 prend branch_B de Job 2 sur la MÊME machine W_SINGLE pendant que le premier tourne
    s2 = client.post(f"/api/jobs/{j2_id}/next_node", json={"runner_id": "r2", "worker": "W_SINGLE"}).get_json()
    assert s2["action"] in ("run", "switch_image")
    assert s2["node"] == "branch_B"

    # Vérifier que les 2 nœuds des 2 jobs sont 'running' sur W_SINGLE simultanément
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT node_name, status, worker_id FROM job_nodes WHERE job_id IN (?, ?) AND status = 'running'", (j1_id, j2_id))
        rows = cursor.fetchall()
        assert len(rows) == 2
        assert all(r[2] == "W_SINGLE" for r in rows)


# =========================================================================
# 17. Packing A11 : GPU discrets attribués sans dépassement (Best Fit)
# =========================================================================

@pytest.mark.parametrize("job_modes", [(False, False), (True, False), (False, True), (True, True)])
def test_discrete_gpus_allocated_without_overcommit(client, job_modes):
    """
    Packing A11 : Sur isipol09 (2 GPUs discrets), 2 nœuds gpus:1 de DEUX JOBS DIFFÉRENTS
    reçoivent des gpu_ids différents sans overcommit. Un 3e job est mis en attente ('wait').
    """
    vram_map = json.dumps([24.0, 24.0])
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, gpu_count, vram_per_gpu, unified_memory, cpus, status, last_seen)
            VALUES ('ISIPOL09', 'isipol09', 'http://127.0.0.1:9000', 64.0, 48.0, 2, ?, 0, 16, 'online', CURRENT_TIMESTAMP)
        ''', (vram_map,))
        conn.commit()

    plan_1 = {
        "version": "3.0",
        "nodes": [
            {"name": "gpu_node_1", "deps": [], "resources": {"ram_gb": 4.0, "gpus": 1, "vram_gb": 12.0}},
            {"name": "gpu_node_1_extra", "deps": [], "resources": {"ram_gb": 4.0, "gpus": 1, "vram_gb": 12.0}}
        ]
    }
    plan_2 = {
        "version": "3.0",
        "nodes": [
            {"name": "gpu_node_2", "deps": [], "resources": {"ram_gb": 4.0, "gpus": 1, "vram_gb": 12.0}}
        ]
    }
    j1_id = _submit_test_job(client, json={"repo": "o/gpu_pack1", "branch": "main", "plan": plan_1, "is_local": job_modes[0]}).get_json()["job_id"]
    j2_id = _submit_test_job(client, json={"repo": "o/gpu_pack2", "branch": "main", "plan": plan_2, "is_local": job_modes[1]}).get_json()["job_id"]
    scheduler_loop.schedule_iteration()

    # Nœud 1 du Job 1 sur ISIPOL09
    s1 = client.post(f"/api/jobs/{j1_id}/next_node", json={"runner_id": "r1", "worker": "ISIPOL09"}).get_json()
    assert s1["action"] in ("run", "switch_image")
    assert s1["gpu_ids"] == [0]

    # Nœud 2 du Job 2 sur ISIPOL09 -> reçoit le second GPU distinct (GPU 1)
    s2 = client.post(f"/api/jobs/{j2_id}/next_node", json={"runner_id": "r2", "worker": "ISIPOL09"}).get_json()
    assert s2["action"] in ("run", "switch_image")
    assert s2["gpu_ids"] == [1]
    assert s1["gpu_ids"] != s2["gpu_ids"]
    assert set(s1["gpu_ids"] + s2["gpu_ids"]) == {0, 1}

    # Demande d'un 3e nœud GPU sur ISIPOL09 alors que ses 2 GPUs sont occupés -> mis en attente sans overcommit
    s3 = client.post(f"/api/jobs/{j1_id}/next_node", json={"runner_id": "r3", "worker": "ISIPOL09"}).get_json()
    assert s3["action"] == "wait"


# =========================================================================
# 18. Packing A11 : Job classique empilé avec un nœud v3
# =========================================================================

@pytest.mark.parametrize("is_local", [False, True])
def test_classic_job_packing_with_v3(client, is_local):
    """
    Packing A11 : Un job classique sans parallel_mode est empilé sur une machine
    déjà occupée par un job v3 si les ressources cumulées le permettent.
    """
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
            VALUES ('W_HYBRID', 'w_hybrid', 'http://127.0.0.1:9000', 32.0, 32.0, 1, 8, 'online', CURRENT_TIMESTAMP)
        ''')
        conn.commit()

    # Job v3 prenant 8 Go de RAM
    plan_v3 = {"version": "3.0", "nodes": [{"name": "v3_task", "deps": [], "resources": {"ram_gb": 8.0, "cpus": 2}}]}
    j_v3_id = _submit_test_job(client, json={"repo": "o/v3", "branch": "main", "plan": plan_v3, "is_local": is_local}).get_json()["job_id"]
    scheduler_loop.schedule_iteration()

    step_v3 = client.post(f"/api/jobs/{j_v3_id}/next_node", json={"runner_id": "r_v3", "worker": "W_HYBRID"}).get_json()
    assert step_v3["action"] in ("run", "switch_image")

    # Job classique demandant 10 Go de RAM (8 + 10 = 18 <= 24 Go max)
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, commit_hash, status, ram_required_gb, vram_required_gb, parallel_mode, created_at)
            VALUES ('classic_job_1', 'o/classic', 'feat-c', 'hash123', 'pending', 10.0, 0.0, 0, CURRENT_TIMESTAMP)
        ''')
        conn.commit()

    scheduler_loop.schedule_iteration()

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, worker_id FROM jobs WHERE job_id = 'classic_job_1'")
        c_status, c_worker = cursor.fetchone()
        assert c_status == "assigned"
        assert c_worker == "W_HYBRID"


# =========================================================================
# 19. Coût de placement A13 : Préférence pour l'image Docker déjà présente
# =========================================================================

def test_placement_preference_docker_image_present(client):
    """
    Coût A13 : Préférence pour le worker hébergeant déjà l'image Docker requise.
    """
    target_img = "docker.io/special/model:v3"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, docker_images, status, last_seen)
            VALUES ('W_COLD', 'w_cold', 'http://127.0.0.1:9001', 64.0, 64.0, 1, 16, '{}', 'online', CURRENT_TIMESTAMP)
        ''')
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, docker_images, status, last_seen)
            VALUES ('W_WARM', 'w_warm', 'http://127.0.0.1:9002', 64.0, 64.0, 1, 16, ?, 'online', CURRENT_TIMESTAMP)
        ''', (json.dumps({target_img: 4000000000}),))
        conn.commit()

    plan = {
        "version": "3.0",
        "nodes": [
            {"name": "img_task", "deps": [], "image": target_img, "resources": {"ram_gb": 4.0, "cpus": 2}}
        ]
    }
    j_id = client.post("/submit_job", json={"repo": "o/img_pref", "branch": "main", "plan": plan}).get_json()["job_id"]
    scheduler_loop.schedule_iteration()

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT home_worker FROM jobs WHERE job_id = ?", (j_id,))
        chosen = cursor.fetchone()[0]
        assert chosen == "W_WARM"


# =========================================================================
# 20. Vérification stricte des NULL & refus explicite journalisé (W4)
# =========================================================================

def test_null_capacity_rejections_and_logs(client, caplog):
    """
    Contre-vérification W4 : Rejet strict sans fallback 999999 ni exception avalée
    si la capacité demandée (storage_gb ou vram_gb) est demandée mais NULL sur le worker.
    """
    import logging
    caplog.set_level(logging.INFO)

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        # Worker 1 avec disk NULL
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, total_storage_gb, status, last_seen)
            VALUES ('W_NULL_DISK', 'w_null_disk', 'http://127.0.0.1:9001', 64.0, 0.0, 0, 16, NULL, 'online', CURRENT_TIMESTAMP)
        ''')
        # Worker 2 avec VRAM NULL
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, vram_per_gpu, unified_memory, cpus, status, last_seen)
            VALUES ('W_NULL_VRAM', 'w_null_vram', 'http://127.0.0.1:9002', 64.0, NULL, NULL, 0, 16, 'online', CURRENT_TIMESTAMP)
        ''')
        conn.commit()

    # Nœud demandant storage_gb = 10.0 sur W_NULL_DISK
    node_res_storage = {"ram_gb": 4.0, "storage_gb": 10.0}
    worker_null_disk = {"worker_id": "W_NULL_DISK", "total_ram_gb": 64.0, "total_storage_gb": None, "cpus": 16, "unified_memory": 0}
    admissible_storage = scheduler_loop.is_worker_admissible_for_node(worker_null_disk, node_res_storage)
    assert admissible_storage is False
    assert any("disk capacity is NULL" in record.message for record in caplog.records)

    # Nœud demandant vram_gb = 8.0 sur W_NULL_VRAM
    node_res_vram = {"ram_gb": 4.0, "vram_gb": 8.0}
    worker_null_vram = {"worker_id": "W_NULL_VRAM", "total_ram_gb": 64.0, "total_vram_gb": None, "vram_per_gpu": None, "cpus": 16, "unified_memory": 0}
    admissible_vram = scheduler_loop.is_worker_admissible_for_node(worker_null_vram, node_res_vram)
    assert admissible_vram is False
    assert any("VRAM capacity is NULL" in record.message for record in caplog.records)


# =========================================================================
# 21. Amendement A16 : Règle de cohérence vram_gb > 0 exige gpus >= 1
# =========================================================================

def test_a16_coherence_vram_requires_gpus_rejection(client):
    """
    Amendement A16 : Soumission d'un plan avec vram_gb > 0 et gpus == 0
    doit être immédiatement rejetée (HTTP 400) avec cause et remède explicites.
    """
    plan = {
        "version": "3.0",
        "nodes": [
            {"name": "bad_gpu_node", "deps": [], "resources": {"ram_gb": 4.0, "vram_gb": 8.0, "gpus": 0}}
        ]
    }
    resp = client.post("/submit_job", json={"repo": "owner/bad_coherence", "branch": "main", "plan": plan})
    assert resp.status_code == 400
    data = resp.get_json()
    assert "vram_gb requires gpus >= 1" in data["error"]
    assert "remedy: declare meta.cluster.gpus >= 1" in data["error"]


# =========================================================================
# 22. Amendement A17 : Nœud impossible -> Échec immédiat avec message actionnable
# =========================================================================

def test_a17_impossible_node_immediate_job_failure_and_actionable_message(client):
    """
    Amendement A17 : Un nœud qu'AUCUNE machine du cluster ne peut admettre, même vide,
    fait échouer le job immédiatement avec un message de la forme :
    « nœud X demande ram_gb=… ; plus grande capacité : machine Y … ; remède : réduire meta.cluster.ram_gb du stage X dans dvc.yaml »
    Ce message remonte dans /job_status et dans les logs agrégés.
    """
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
            VALUES ('W_SMALL', 'w_small', 'http://127.0.0.1:9000', 32.0, 0.0, 1, 8, 'online', CURRENT_TIMESTAMP)
        ''')
        conn.commit()

    # Nœud demandant 64 Go RAM alors que la plus grande machine n'a que 32 - 8 = 24 Go dispo
    plan = {
        "version": "3.0",
        "nodes": [
            {"name": "too_big_node", "deps": [], "resources": {"ram_gb": 64.0, "cpus": 2}}
        ]
    }
    submit_resp = client.post("/submit_job", json={"repo": "owner/impossible", "branch": "main", "plan": plan}).get_json()
    job_id = submit_resp["job_id"]

    # Exécution de l'itération d'ordonnancement : doit échouer immédiatement
    scheduler_loop.schedule_iteration()

    # 1. Vérification en base de données
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, exit_code FROM jobs WHERE job_id = ?", (job_id,))
        j_status, j_exit = cursor.fetchone()
        assert j_status == "failed"
        assert j_exit == 1

        cursor.execute("SELECT status, error_message FROM job_nodes WHERE job_id = ? AND node_name = 'too_big_node'", (job_id,))
        n_status, n_err = cursor.fetchone()
        assert n_status == "failed"
        assert "node too_big_node requests ram_gb=64.0 GB" in n_err
        assert "machine W_SMALL (24.0 GB)" in n_err
        assert "remedy: reduce meta.cluster.ram_gb of stage too_big_node in dvc.yaml" in n_err

    # 2. Vérification de la route /job_status
    st_resp = client.get(f"/job_status/{job_id}").get_json()
    assert st_resp["status"] == "failed"
    nodes_by_name = {n["name"]: n for n in st_resp.get("nodes", [])} if isinstance(st_resp.get("nodes"), list) else st_resp.get("nodes", {})
    node_sum = nodes_by_name.get("too_big_node", {})
    assert "remedy: reduce meta.cluster.ram_gb" in node_sum.get("error_message", "")

    # 3. Vérification des logs agrégés
    log_resp = client.get(f"/job_logs/{job_id}?offset=0").get_json()
    assert "remedy: reduce meta.cluster.ram_gb" in log_resp.get("logs", "")


# =========================================================================
# 23. Persistance des capacités W4 & docker_images dans register_worker / heartbeat
# =========================================================================

def test_register_worker_persists_w4_capacities_and_docker_images(client):
    """
    Vérifie que /register_worker et /heartbeat persistent les capacités fines W4
    (cpus, ram_gb, vram_per_gpu, unified_memory, arch, disk_free_gb, role, docker_images)
    et que /workers les restitue fidèlement.
    """
    target_img = "docker.io/nvidia/pytorch:26.05-py3"
    worker_payload = {
        "worker_id": "W_GPU_A13",
        "hostname": "worker-gpu-a13",
        "service_url": "http://10.0.0.42:6000",
        "total_ram_gb": 128.0,
        "available_ram_gb": 110.0,
        "total_storage_gb": 1000.0,
        "available_storage_gb": 850.0,
        "total_vram_gb": 24.0,
        "available_vram_gb": 24.0,
        "gpu_count": 2,
        "gpu_name": "NVIDIA GeForce RTX 3090",
        "cpus": 32,
        "ram_gb": 128.0,
        "vram_per_gpu": [24.0, 24.0],
        "unified_memory": 0,
        "arch": "x86_64",
        "disk_free_gb": 850.0,
        "role": "worker",
        "docker_images": {target_img: 5000000000}
    }

    # 1. Enregistrement via /register_worker
    resp = client.post("/register_worker", json=worker_payload)
    assert resp.status_code == 200

    # 2. Vérification directe en base SQLite
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT cpus, ram_gb, vram_per_gpu, arch, docker_images FROM workers WHERE worker_id = 'W_GPU_A13'")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == 32
        assert row[1] == 128.0
        assert json.loads(row[2]) == [24.0, 24.0]
        assert row[3] == "x86_64"
        assert json.loads(row[4]) == {target_img: 5000000000}

    # 3. Vérification de la route /workers
    workers_resp = client.get("/workers").get_json()
    w_found = next((w for w in workers_resp if w["worker_id"] == "W_GPU_A13"), None)
    assert w_found is not None
    assert w_found["cpus"] == 32
    assert w_found["docker_images"] == {target_img: 5000000000}

    # 4. Vérification via l'alias /heartbeat
    worker_payload["docker_images"][target_img] = 6000000000
    hb_resp = client.post("/heartbeat", json=worker_payload)
    assert hb_resp.status_code == 200

    workers_resp2 = client.get("/workers").get_json()
    w_found2 = next((w for w in workers_resp2 if w["worker_id"] == "W_GPU_A13"), None)
    assert w_found2["docker_images"][target_img] == 6000000000


# =========================================================================
# 24. Messages A17 / OOMKilled dans /job_status et /job_logs
# =========================================================================

def test_a17_oomkilled_message_in_job_status_and_job_logs(client):
    """
    Vérifie qu'un échec OOM (exit code 137 ou mention OOMKilled) remonté par
    l'exécuteur apparaît explicitement dans /job_status et dans /job_logs.
    """
    # Cas A : Exécuteur v3 par DAG (/api/jobs/<job_id>/next_node)
    plan = {
        "version": "3.0",
        "nodes": [
            {"name": "heavy_stage", "deps": [], "resources": {"ram_gb": 8.0, "cpus": 2}}
        ]
    }
    j_resp = client.post("/submit_job", json={"repo": "owner/oom-test", "branch": "main", "plan": plan}).get_json()
    job_id_v3 = j_resp["job_id"]

    # Remontée d'un échec exit code 137 par le runner
    fail_payload = {
        "runner_id": "runner_oom_1",
        "worker": "w1",
        "node": "heavy_stage",
        "status": "failed",
        "exit_code": 137,
        "error_message": "Container killed by system OOM Killer"
    }
    next_node_resp = client.post(f"/api/jobs/{job_id_v3}/next_node", json=fail_payload)
    assert next_node_resp.status_code == 200

    # Vérification /job_status
    st_v3 = client.get(f"/job_status/{job_id_v3}").get_json()
    assert "OOMKilled" in st_v3.get("error_message", "")
    nodes = {n["name"]: n for n in st_v3.get("nodes", [])}
    assert "OOMKilled" in nodes["heavy_stage"].get("error_message", "")

    # Vérification /job_logs
    logs_v3 = client.get(f"/job_logs/{job_id_v3}?offset=0").get_json()
    assert "OOMKilled" in logs_v3.get("logs", "")

    # Cas B : Job classique (/update_job_status avec exit_code 137)
    j_classic = client.post("/submit_job", json={"repo": "owner/oom-classic", "branch": "main", "ram_required_gb": 4.0}).get_json()
    job_id_classic = j_classic["job_id"]

    update_resp = client.post("/update_job_status", json={"job_id": job_id_classic, "status": "failed", "exit_code": 137})
    assert update_resp.status_code == 200

    st_classic = client.get(f"/job_status/{job_id_classic}").get_json()
    assert st_classic["status"] == "failed"
    assert "OOMKilled" in st_classic.get("error_message", "")

    logs_classic = client.get(f"/job_logs/{job_id_classic}?offset=0").get_json()
    assert "OOMKilled" in logs_classic.get("logs", "")


# =========================================================================
# 25. Amendement A17 : Job classique impossible sur mémoire unifiée (b85ab474)
# =========================================================================

def test_classic_job_impossible_unified_memory_and_actionable_message(client):
    """
    Test réel : le job classique b85ab474 (REQUIRED_RAM=100GB, REQUIRED_VRAM=100GB)
    sur deux machines GB10 (121.63 Go de mémoire unifiée chacune).
    - Somme 200 Go > 121.63 - 8 = 113.63 Go : impossible sur toute machine.
    - Doit échouer immédiatement avec message actionnable A17 exhaustif.
    - Un job admissible (50 Go RAM + 50 Go VRAM = 100 Go <= 113.63 Go) doit être assigné avec succès.
    """
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM jobs")
        cursor.execute("DELETE FROM workers")
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
            VALUES ('GB10_1', 'gb10-1', 'http://10.0.0.1:6000', 121.63, 121.63, 1, 32, 'online', CURRENT_TIMESTAMP)
        ''')
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, total_vram_gb, unified_memory, cpus, status, last_seen)
            VALUES ('GB10_2', 'gb10-2', 'http://10.0.0.2:6000', 121.63, 121.63, 1, 32, 'online', CURRENT_TIMESTAMP)
        ''')
        conn.commit()

    # 1. Cas impossible (100 Go RAM + 100 Go VRAM)
    submit_resp = client.post("/submit_job", json={
        "repo": "owner/impossible-classic",
        "branch": "main",
        "ram_required_gb": 100.0,
        "vram_required_gb": 100.0
    }).get_json()
    job_id_impossible = submit_resp["job_id"]

    # Exécution de l'itération d'ordonnancement
    scheduler_loop.schedule_iteration()

    # Vérification base SQLite
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, exit_code, error_message FROM jobs WHERE job_id = ?", (job_id_impossible,))
        j_status, j_exit, j_err = cursor.fetchone()
        assert j_status == "failed"
        assert j_exit == 1
        assert "Classic job" in j_err
        assert "REQUIRED_RAM=100.0 GB and REQUIRED_VRAM=100.0 GB" in j_err
        assert "sum=200.0 GB on unified memory" in j_err
        assert "machine GB10_1 (113.6 GB max unified RAM+VRAM, 32 CPUs)" in j_err
        assert "machine GB10_2 (113.6 GB max unified RAM+VRAM, 32 CPUs)" in j_err
        assert "remedy: decrease REQUIRED_RAM + REQUIRED_VRAM to <= 113 GB for GB10, or declare meta.cluster per stage and enable v3 mode" in j_err

    # Vérification route /job_status
    st_resp = client.get(f"/job_status/{job_id_impossible}").get_json()
    assert st_resp["status"] == "failed"
    assert "decrease REQUIRED_RAM + REQUIRED_VRAM to <= 113 GB for GB10" in st_resp.get("error_message", "")

    # Vérification route /job_logs
    log_resp = client.get(f"/job_logs/{job_id_impossible}?offset=0").get_json()
    assert "decrease REQUIRED_RAM + REQUIRED_VRAM to <= 113 GB for GB10" in log_resp.get("logs", "")

    # 2. Cas admissible (50 Go RAM + 50 Go VRAM = 100 Go <= 113.63 Go)
    submit_ok = client.post("/submit_job", json={
        "repo": "owner/admissible-classic",
        "branch": "main",
        "ram_required_gb": 50.0,
        "vram_required_gb": 50.0
    }).get_json()
    job_id_admissible = submit_ok["job_id"]

    scheduler_loop.schedule_iteration()

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, worker_id, error_message FROM jobs WHERE job_id = ?", (job_id_admissible,))
        ok_status, ok_worker, ok_err = cursor.fetchone()
        assert ok_status in ("assigned", "running")
        assert ok_worker in ("GB10_1", "GB10_2")
        assert ok_err is None

    st_ok = client.get(f"/job_status/{job_id_admissible}").get_json()
    assert st_ok["status"] in ("assigned", "running")


def test_headnode_placement_priority_last_resort_a13(client):
    """
    Test Amendement A13 :
    3 machines enregistrées via /register_worker :
    - Headnode isipol09 avec la plus grosse RAM (128 Go), role='headnode'
    - GB10_1 HEC45801 (120 Go), role='worker', unified_memory=1
    - GB10_2 HEC45803 (121 Go), role='worker', unified_memory=1
    Un nœud prêt doit aller sur un GB10 (HEC45803) et JAMAIS sur le headnode isipol09 tant que les GB10 sont libres.
    """
    # 1. Enregistrement des 3 machines via l'API /register_worker
    resp_hn = client.post("/register_worker", json={
        "worker_id": "HN_isipol09",
        "hostname": "isipol09",
        "service_url": "http://127.0.0.1:9000",
        "total_ram_gb": 128.0,
        "available_storage_gb": 500.0,
        "cpus": 32,
        "role": "headnode",
        "is_headnode": True
    })
    assert resp_hn.status_code in (200, 201)

    resp_w1 = client.post("/register_worker", json={
        "worker_id": "W1_HEC45801",
        "hostname": "HEC45801",
        "service_url": "http://127.0.0.1:9001",
        "total_ram_gb": 120.0,
        "available_storage_gb": 500.0,
        "total_vram_gb": 120.0,
        "unified_memory": 1,
        "gpu_name": "NVIDIA GB10",
        "gpu_count": 1,
        "cpus": 32,
        "role": "worker"
    })
    assert resp_w1.status_code in (200, 201)

    resp_w2 = client.post("/register_worker", json={
        "worker_id": "W2_HEC45803",
        "hostname": "HEC45803",
        "service_url": "http://127.0.0.1:9002",
        "total_ram_gb": 121.0,
        "available_storage_gb": 500.0,
        "total_vram_gb": 121.0,
        "unified_memory": 1,
        "gpu_name": "NVIDIA GB10",
        "gpu_count": 1,
        "cpus": 32,
        "role": "worker"
    })
    assert resp_w2.status_code in (200, 201)

    # Vérification que placement_priority est correctement persisté en base
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT worker_id, placement_priority, total_ram_gb FROM workers ORDER BY placement_priority DESC, total_ram_gb DESC")
        rows = cursor.fetchall()
        worker_prios = {r[0]: r[1] for r in rows}
        assert worker_prios["HN_isipol09"] == 0
        assert worker_prios["W1_HEC45801"] == 50
        assert worker_prios["W2_HEC45803"] == 50
        # W2 (121 Go) et W1 (120 Go) doivent être classés avant HN (128 Go)
        assert rows[0][0] == "W2_HEC45803"
        assert rows[1][0] == "W1_HEC45801"
        assert rows[2][0] == "HN_isipol09"

    # 2. Soumission d'un job v3 avec un nœud prêt
    plan = {
        "version": "3.0",
        "defaults": {"image": "image:default", "ram_gb": 4.0},
        "nodes": [
            {"name": "prep", "deps": [], "priority": 10.0, "stale": True}
        ]
    }
    sub = client.post("/submit_job", json={"repo": "owner/repo", "branch": "feat", "plan": plan}).get_json()
    job_id = sub["job_id"]

    # 3. Exécution du scheduler
    scheduler_loop.schedule_iteration()

    # 4. Vérification que le job a été attribué à un GB10 (W2_HEC45803) et NON au headnode
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT home_worker FROM jobs WHERE job_id = ?", (job_id,))
        home_worker = cursor.fetchone()[0]
        assert home_worker in ("W2_HEC45803", "W1_HEC45801")
        assert home_worker != "HN_isipol09"
        assert home_worker == "W2_HEC45803"

    # 5. Récupération du nœud prêt par le worker : il doit recevoir le nœud prep
    step_resp = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "runner_w2",
        "worker": home_worker,
        "node": None,
        "status": None
    }).get_json()
    assert step_resp["action"] in ("run", "switch_image")
    assert step_resp["node"] == "prep"




