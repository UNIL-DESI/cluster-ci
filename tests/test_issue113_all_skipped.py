"""
tests/test_issue113_all_skipped.py - Validation de l'Issue #113 :
La table SQLite jobs doit passer son statut à 'completed' avec finished_at = CURRENT_TIMESTAMP
et exit_code = 0 lors de l'instanciation d'un job 100% skipped.
"""

import os
import sys
import uuid
import pytest

sched_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "scheduler")
if sched_dir not in sys.path:
    sys.path.insert(0, sched_dir)

import persistence
import headnode_service


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Initialise une base SQLite temporaire et isolée."""
    db_file = str(tmp_path / f"test_cluster_issue113_{uuid.uuid4().hex[:8]}.db")
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


def test_issue113_job_100_percent_skipped_marked_completed(client):
    """
    Vérifie qu'un job DAG dont tous les nœuds ont stale=False est instantanément marqué
    'completed' dans la table SQLite jobs avec finished_at non null et exit_code=0 dès sa soumission.
    """
    plan = {
        "nodes": [
            {
                "name": "data_prep",
                "deps": [],
                "resources": {"cpus": 1, "ram_gb": 2.0},
                "stale": False,
            },
            {
                "name": "model_train",
                "deps": ["data_prep"],
                "resources": {"cpus": 2, "ram_gb": 4.0},
                "stale": False,
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
    data = sub_resp.get_json()
    job_id = data["job_id"]
    assert data["status"] == "completed"

    # Vérification via l'API GET /job_status/<job_id>
    st_resp = client.get(f"/job_status/{job_id}").get_json()
    assert st_resp["status"] == "completed"

    # Vérification directe dans la table SQLite jobs
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, finished_at, exit_code FROM jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        assert row is not None
        assert row["status"] == "completed"
        assert row["finished_at"] is not None
        assert row["exit_code"] == 0


def test_issue113_job_with_active_nodes_remains_pending(client):
    """
    Vérifie qu'un job DAG avec au moins un nœud stale=True reste 'pending' en base SQLite.
    """
    plan = {
        "nodes": [
            {
                "name": "data_prep",
                "deps": [],
                "resources": {"cpus": 1, "ram_gb": 2.0},
                "stale": False,
            },
            {
                "name": "model_train",
                "deps": ["data_prep"],
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
    data = sub_resp.get_json()
    job_id = data["job_id"]
    assert data["status"] == "pending"

    # Vérification directe dans la table SQLite jobs
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, finished_at, exit_code FROM jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        assert row is not None
        assert row["status"] == "pending"
        assert row["finished_at"] is None
        assert row["exit_code"] is None
