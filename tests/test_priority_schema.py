import os
import sqlite3
import pytest
from src.scheduler.persistence import init_db, init_job_nodes_from_plan

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_migration.db"
    orig_db = os.environ.get("CLUSTER_DB_PATH")
    os.environ["CLUSTER_DB_PATH"] = str(db_file)
    yield str(db_file)
    if orig_db is not None:
        os.environ["CLUSTER_DB_PATH"] = orig_db
    else:
        os.environ.pop("CLUSTER_DB_PATH", None)

def test_migration_idempotence_and_columns(temp_db):
    # Première initialisation
    init_db()
    
    # Seconde initialisation (doit être idempotente sans erreur)
    init_db()

    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()

    # Vérifier les colonnes de jobs
    cursor.execute("PRAGMA table_info(jobs)")
    job_cols = {row[1] for row in cursor.fetchall()}
    assert "scheduling_priority" in job_cols

    # Vérifier les colonnes de job_nodes
    cursor.execute("PRAGMA table_info(job_nodes)")
    node_cols = {row[1] for row in cursor.fetchall()}
    assert "scheduling_priority" in node_cols
    assert "preempt_count" in node_cols
    assert "preempted_at" in node_cols
    assert "preempted_by" in node_cols

    conn.close()

def test_migration_on_legacy_db_without_new_columns(temp_db):
    # Simuler une base existante legacy sans les nouvelles colonnes
    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'pending'
        )
    """)
    cursor.execute("""
        CREATE TABLE job_nodes (
            job_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            PRIMARY KEY (job_id, node_name)
        )
    """)
    cursor.execute("""
        CREATE TABLE workers (
            worker_id TEXT PRIMARY KEY
        )
    """)
    cursor.execute("INSERT INTO jobs (job_id) VALUES ('legacy-job')")
    cursor.execute("INSERT INTO job_nodes (job_id, node_name) VALUES ('legacy-job', 'stage1')")
    conn.commit()
    conn.close()

    # Lancer init_db() sur cette base legacy
    init_db()

    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT scheduling_priority FROM jobs WHERE job_id = 'legacy-job'")
    assert cursor.fetchone()[0] == 'normal'

    cursor.execute("SELECT scheduling_priority, preempt_count FROM job_nodes WHERE job_id = 'legacy-job'")
    row = cursor.fetchone()
    assert row[0] == 'normal'
    assert row[1] == 0
    conn.close()

def test_init_job_nodes_from_plan_scheduling_priority(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO jobs (job_id, status) VALUES ('job-plan', 'pending')")
    conn.commit()
    conn.close()

    plan_data = {
        "nodes": [
            {
                "name": "node-high",
                "scheduling_priority": "high",
                "deps": [],
                "resources": {"ram_gb": 4}
            },
            {
                "name": "node-low",
                "resources": {"priority": "low", "ram_gb": 2},
                "deps": []
            },
            {
                "name": "node-default",
                "deps": []
            }
        ],
        "defaults": {}
    }

    init_job_nodes_from_plan("job-plan", plan_data)

    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT node_name, scheduling_priority FROM job_nodes WHERE job_id = 'job-plan' ORDER BY node_name")
    rows = dict(cursor.fetchall())
    assert rows["node-high"] == "high"
    assert rows["node-low"] == "low"
    assert rows["node-default"] == "normal"
    conn.close()
