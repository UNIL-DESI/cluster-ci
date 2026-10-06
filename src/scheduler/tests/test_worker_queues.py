"""
test_worker_queues.py - Tests backend et API pour la file d'attente par machine.

Vérifie :
1. Base SQLite temporaire avec 2 workers en ligne.
2. Répartition des nœuds ready/pending avec et sans contraintes de placement (whitelist allowed_workers).
3. Ordre réel de sélection du scheduler : ready avant pending, FIFO par created_at du job, priority de DAG.
4. Jobs locaux (is_local = 1) : exposition stricte des métadonnées autorisées sans contenu de fichier.
5. Endpoints HTTP GET /api/workers/queues et /api/workers/<worker_id>/queue.
"""

import os
import sys
import json
import uuid
import pytest
from datetime import datetime, timezone

sched_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sched_dir not in sys.path:
    sys.path.insert(0, sched_dir)

import persistence  # noqa: E402
import headnode_service  # noqa: E402
from headnode_service import app, get_worker_queues, format_waiting_time  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Initialise une base SQLite temporaire et isolée pour chaque test."""
    db_file = str(tmp_path / f"test_cluster_queues_{uuid.uuid4().hex[:8]}.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", db_file)
    monkeypatch.setattr(headnode_service, 'REPOS_DIR', str(tmp_path / 'repositories'))
    persistence.DB_PATH = db_file
    persistence.init_db()
    yield db_file


def test_format_waiting_time():
    assert format_waiting_time(10) == "10s"
    assert format_waiting_time(59) == "59s"
    assert format_waiting_time(60) == "1m"
    assert format_waiting_time(75) == "1m 15s"
    assert format_waiting_time(3600) == "1h 00m"
    assert format_waiting_time(3665) == "1h 01m"


def test_worker_queues_placement_and_ordering():
    """Vérifie la répartition et l'ordre des nœuds en file d'attente sur 2 workers."""
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()

        # 1. Deux workers en ligne
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, status, cpus, total_ram_gb, total_vram_gb, last_seen, placement_priority)
            VALUES 
                ('W1', 'hec45801', 'online', 16, 64.0, 24.0, ?, 100),
                ('W2', 'hec45802', 'online', 16, 64.0, 24.0, ?, 100)
        ''', (now_str, now_str))

        # 2. Job 1 (v3 parallel, public, running) créé il y a 10 minutes
        job1_id = "job-v3-001"
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, parallel_mode, status, is_local, created_at)
            VALUES (?, 'owner/repo1', 'main', 1, 'running', 0, datetime('now', '-600 seconds'))
        ''', (job1_id,))

        # Nœud sans contrainte de placement -> admissible sur W1 et W2
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, priority, resources)
            VALUES (?, 'prep', 'ready', 5.0, ?)
        ''', (job1_id, json.dumps({"cpus": 4, "ram_gb": 8.0})))

        # Nœud restreint à W1 -> admissible UNIQUEMENT sur W1
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, priority, resources)
            VALUES (?, 'train_w1', 'ready', 4.0, ?)
        ''', (job1_id, json.dumps({"cpus": 4, "ram_gb": 8.0, "workers": ["W1"]})))

        # Nœud restreint à W2 -> admissible UNIQUEMENT sur W2
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, priority, resources)
            VALUES (?, 'train_w2', 'ready', 4.0, ?)
        ''', (job1_id, json.dumps({"cpus": 4, "ram_gb": 8.0, "workers": ["W2"]})))

        # Nœud pending -> admissible sur W1 et W2, mais passe après ready
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, priority, resources, deps)
            VALUES (?, 'eval', 'pending', 2.0, ?, ?)
        ''', (job1_id, json.dumps({"cpus": 2, "ram_gb": 4.0}), json.dumps(["train_w1", "train_w2"])))

        # 3. Job 2 (local job, is_local=1, pending) créé il y a 2 minutes
        job2_id = "job-local-002"
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, parallel_mode, status, is_local, created_at)
            VALUES (?, 'caritas/sensitive', 'feature', 1, 'pending', 1, datetime('now', '-120 seconds'))
        ''', (job2_id,))

        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, priority, resources)
            VALUES (?, 'local_extract', 'ready', 1.0, ?)
        ''', (job2_id, json.dumps({"cpus": 4, "ram_gb": 8.0})))

        # 4. Job 3 (terminé) -> ses nœuds ne doivent PAS figurer dans la file
        job3_id = "job-finished-003"
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, parallel_mode, status, is_local, created_at)
            VALUES (?, 'owner/old', 'main', 1, 'completed', 0, datetime('now', '-3600 seconds'))
        ''', (job3_id,))
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, priority, resources)
            VALUES (?, 'old_node', 'ready', 1.0, ?)
        ''', (job3_id, json.dumps({"cpus": 1, "ram_gb": 1.0})))

        conn.commit()

    # Exécution du calcul des files d'attente par machine
    queues = get_worker_queues()

    assert "W1" in queues, "W1 doit être présent dans les files d'attente"
    assert "W2" in queues, "W2 doit être présent dans les files d'attente"

    w1_stages = [n["stage"] for n in queues["W1"]]
    w2_stages = [n["stage"] for n in queues["W2"]]

    # Vérification de la répartition des contraintes de placement
    assert "train_w1" in w1_stages, "train_w1 doit être dans la file de W1"
    assert "train_w1" not in w2_stages, "train_w1 ne doit PAS être dans la file de W2 (contrainte allowed_workers)"

    assert "train_w2" in w2_stages, "train_w2 doit être dans la file de W2"
    assert "train_w2" not in w1_stages, "train_w2 ne doit PAS être dans la file de W1 (contrainte allowed_workers)"

    assert "prep" in w1_stages and "prep" in w2_stages, "prep (sans contrainte) doit être éligible sur les 2 workers"
    assert "eval" in w1_stages and "eval" in w2_stages, "eval (sans contrainte) doit être éligible sur les 2 workers"
    assert "local_extract" in w1_stages and "local_extract" in w2_stages

    # Vérification de l'ordre de sélection du scheduler :
    # 1. Job 1 créé il y a 600s vs Job 2 créé il y a 120s -> Job 1 d'abord (FIFO)
    # 2. Au sein de Job 1 : nœuds ready ('prep', 'train_w1') avant pending ('eval')
    # 3. Pour W1 : 'prep' (priority 5.0) avant 'train_w1' (priority 4.0)
    w1_nodes = queues["W1"]
    assert w1_nodes[0]["stage"] == "prep"
    assert w1_nodes[1]["stage"] == "train_w1"
    assert w1_nodes[2]["stage"] == "local_extract"  # Job 2 (ready) passe avant le pending de Job 1 ou après les ready FIFO
    assert w1_nodes[3]["stage"] == "eval"  # pending en queue

    # Vérification stricte des métadonnées du job local (confidentialité : pas de fichiers)
    local_nodes = [n for n in queues["W1"] if n["job_id"] == job2_id]
    assert len(local_nodes) == 1
    ln = local_nodes[0]
    assert ln["is_local"] == 1
    assert ln["repo"] == "caritas/sensitive"
    assert ln["branch"] == "feature"
    assert ln["job_id_short"] == "job-loca"
    assert ln["status"] == "ready"
    assert ln["waiting_seconds"] >= 100.0
    assert "m" in ln["waiting_time"] or "s" in ln["waiting_time"]

    # Le job terminé n'apparaît nulle part
    assert "old_node" not in w1_stages
    assert "old_node" not in w2_stages


def test_worker_queues_api_endpoints():
    """Vérifie les endpoints REST Flask GET /api/workers/queues et /api/workers/<id>/queue."""
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, status, cpus, total_ram_gb, last_seen)
            VALUES ('W_API', 'api-node', 'online', 8, 32.0, ?)
        ''', (now_str,))
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, parallel_mode, status, is_local, created_at)
            VALUES ('job-api', 'test/api', 'main', 1, 'running', 0, datetime('now', '-30 seconds'))
        ''', )
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, priority, resources)
            VALUES ('job-api', 'api_stage', 'ready', 1.0, ?)
        ''', (json.dumps({"cpus": 2, "ram_gb": 4.0}),))
        conn.commit()

    client = app.test_client()

    # 1. Non authentifié sans session ni token -> 401
    resp = client.get('/api/workers/queues')
    assert resp.status_code == 401

    # 2. Authentifié via session utilisateur
    with client.session_transaction() as sess:
        sess['user'] = {'login': 'tester', 'name': 'Test User'}

    resp = client.get('/api/workers/queues')
    assert resp.status_code == 200
    data = resp.get_json()
    assert "W_API" in data
    assert len(data["W_API"]) == 1
    assert data["W_API"][0]["stage"] == "api_stage"

    # 3. Endpoint pour un worker individuel
    resp_single = client.get('/api/workers/W_API/queue')
    assert resp_single.status_code == 200
    single_data = resp_single.get_json()
    assert isinstance(single_data, list)
    assert len(single_data) == 1
    assert single_data[0]["node_name"] == "api_stage"
