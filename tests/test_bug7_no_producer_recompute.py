"""
Tests unitaires ciblés pour le Bug 7 : Pas de rejeu du producteur quand les artefacts existent sur un pair en ligne.
Vérifie :
1. Si un pair en ligne détient l'artefact manquant : le producteur reste 'done', le consommateur repasse 'ready' (retry_fetch).
2. Si aucun pair ne le détient : le producteur passe 'ready' avec raison explicite 'outputs_missing_no_peer_cache'.
3. Si le producteur a déjà été relancé : échec explicite 'retry_exhausted'.
"""

import json
import sqlite3
import pytest

from src.scheduler.persistence import (
    get_db_conn,
    init_db,
    handle_missing_deps,
)
from src.scheduler.artifact_registry import ensure_schema


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_cluster_bug7.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", db_file)
    init_db()
    with sqlite3.connect(db_file) as conn:
        ensure_schema(conn)
    return db_file


def test_handle_missing_deps_peer_available_no_producer_recompute(test_db):
    """Quand un worker en ligne détient l'artefact, le producteur NE DOIT PAS être replanifié."""
    job_id = "job-p2p-7"
    prod_name = "producer_stage"
    cons_name = "consumer_stage"
    missing_file = "data/model_heavy.pt"
    md5_hash = "9876543210abcdef9876543210abcdef"

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO jobs (job_id, status, parallel_mode) VALUES (?, 'running', 1)", (job_id,))
        # Worker pair en ligne
        cursor.execute("""
            INSERT INTO workers (worker_id, hostname, service_url, status)
            VALUES ('worker-peer', 'hec45801', 'http://hec45801:6000', 'online')
        """)
        # Artefact enregistré sur worker-peer
        cursor.execute("""
            INSERT INTO node_artifacts (job_id, node_name, md5, is_dir, size_bytes, worker_id, path)
            VALUES ('job-p2p-7', ?, ?, 0, 1048576, 'worker-peer', ?)
        """, (prod_name, md5_hash, missing_file))
        # Nœud producteur : initialement 'done'
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, out_paths, missing_deps_retried)
            VALUES (?, ?, 'done', ?, 0)
        """, (job_id, prod_name, json.dumps([{"path": missing_file, "md5": md5_hash}])))
        # Nœud consommateur : initialement 'running'
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, deps, dep_paths, missing_deps_retried)
            VALUES (?, ?, 'running', ?, ?, 0)
        """, (job_id, cons_name, json.dumps([prod_name]), json.dumps([missing_file])))
        conn.commit()

    # Consommateur rapporte le fichier manquant
    res = handle_missing_deps(job_id, cons_name, [missing_file])

    assert res["success"] is True
    assert res.get("action") == "retry_fetch"
    assert "sources" in res

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, stale_reason, missing_deps_retried FROM job_nodes WHERE job_id = ? AND node_name = ?", (job_id, prod_name))
        prod_row = cursor.fetchone()
        assert prod_row["status"] == "done", "Le producteur doit impérativement rester 'done' !"
        assert prod_row["missing_deps_retried"] == 0

        cursor.execute("SELECT status, missing_deps_retried FROM job_nodes WHERE job_id = ? AND node_name = ?", (job_id, cons_name))
        cons_row = cursor.fetchone()
        assert cons_row["status"] == "ready", "Le consommateur doit repasser en 'ready' pour retenter le fetch !"
        assert cons_row["missing_deps_retried"] == 1


def test_handle_missing_deps_no_peer_reschedules_producer_with_reason(test_db):
    """Quand AUCUN worker en ligne ne détient l'artefact, le producteur est replanifié avec raison explicite."""
    job_id = "job-no-peer-7"
    prod_name = "producer_stage"
    cons_name = "consumer_stage"
    missing_file = "data/model_heavy.pt"
    md5_hash = "abcdef1122334455abcdef1122334455"

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO jobs (job_id, status, parallel_mode) VALUES (?, 'running', 1)", (job_id,))
        # Zéro worker détenant l'artefact en ligne
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, out_paths, missing_deps_retried)
            VALUES (?, ?, 'done', ?, 0)
        """, (job_id, prod_name, json.dumps([{"path": missing_file, "md5": md5_hash}])))
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, deps, dep_paths, missing_deps_retried)
            VALUES (?, ?, 'running', ?, ?, 0)
        """, (job_id, cons_name, json.dumps([prod_name]), json.dumps([missing_file])))
        conn.commit()

    res = handle_missing_deps(job_id, cons_name, [missing_file])

    assert res["success"] is True
    assert res.get("retriggered_node") == prod_name
    assert res.get("reason") == "outputs_missing_no_peer_cache"

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, stale_reason, missing_deps_retried FROM job_nodes WHERE job_id = ? AND node_name = ?", (job_id, prod_name))
        prod_row = cursor.fetchone()
        assert prod_row["status"] == "ready"
        assert prod_row["stale_reason"] == "outputs_missing_no_peer_cache"
        assert prod_row["missing_deps_retried"] == 1

        cursor.execute("SELECT status FROM job_nodes WHERE job_id = ? AND node_name = ?", (job_id, cons_name))
        cons_row = cursor.fetchone()
        assert cons_row["status"] == "pending"


def test_handle_missing_deps_peer_offline_reschedules_producer(test_db):
    """Si le pair détenteur est hors ligne ('offline'), il n'est pas qualifié et le producteur est replanifié."""
    job_id = "job-offline-peer-7"
    prod_name = "producer_stage"
    cons_name = "consumer_stage"
    missing_file = "data/model_heavy.pt"
    md5_hash = "offline1234567890abcdef123456789"

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO jobs (job_id, status, parallel_mode) VALUES (?, 'running', 1)", (job_id,))
        # Worker pair HORS LIGNE
        cursor.execute("""
            INSERT INTO workers (worker_id, hostname, service_url, status)
            VALUES ('worker-offline', 'hec45802', 'http://hec45802:6000', 'offline')
        """)
        cursor.execute("""
            INSERT INTO node_artifacts (job_id, node_name, md5, is_dir, size_bytes, worker_id, path)
            VALUES (?, ?, ?, 0, 1048576, 'worker-offline', ?)
        """, (job_id, prod_name, md5_hash, missing_file))
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, out_paths, missing_deps_retried)
            VALUES (?, ?, 'done', ?, 0)
        """, (job_id, prod_name, json.dumps([{"path": missing_file, "md5": md5_hash}])))
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, deps, dep_paths, missing_deps_retried)
            VALUES (?, ?, 'running', ?, ?, 0)
        """, (job_id, cons_name, json.dumps([prod_name]), json.dumps([missing_file])))
        conn.commit()

    res = handle_missing_deps(job_id, cons_name, [missing_file])

    assert res["success"] is True
    assert res.get("retriggered_node") == prod_name
    assert res.get("reason") == "outputs_missing_no_peer_cache"

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, stale_reason FROM job_nodes WHERE job_id = ? AND node_name = ?", (job_id, prod_name))
        row = cursor.fetchone()
        assert row["status"] == "ready"
        assert row["stale_reason"] == "outputs_missing_no_peer_cache"


def test_handle_missing_deps_repeated_fetch_failures_reschedules_producer(test_db):
    """Si le fetch P2P échoue de façon répétée (borne missing_deps_retried >= 2 atteinte), le producteur est replanifié."""
    job_id = "job-repeated-fail-7"
    prod_name = "producer_stage"
    cons_name = "consumer_stage"
    missing_file = "data/model_heavy.pt"
    md5_hash = "repeated1234567890abcdef12345678"

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO jobs (job_id, status, parallel_mode) VALUES (?, 'running', 1)", (job_id,))
        # Worker pair en ligne
        cursor.execute("""
            INSERT INTO workers (worker_id, hostname, service_url, status)
            VALUES ('worker-online', 'hec45803', 'http://hec45803:6000', 'online')
        """)
        cursor.execute("""
            INSERT INTO node_artifacts (job_id, node_name, md5, is_dir, size_bytes, worker_id, path)
            VALUES (?, ?, ?, 0, 1048576, 'worker-online', ?)
        """, (job_id, prod_name, md5_hash, missing_file))
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, out_paths, missing_deps_retried)
            VALUES (?, ?, 'done', ?, 0)
        """, (job_id, prod_name, json.dumps([{"path": missing_file, "md5": md5_hash}])))
        # Consommateur qui a DÉJÀ atteint la borne de 2 retries
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, deps, dep_paths, missing_deps_retried)
            VALUES (?, ?, 'running', ?, ?, 2)
        """, (job_id, cons_name, json.dumps([prod_name]), json.dumps([missing_file])))
        conn.commit()

    res = handle_missing_deps(job_id, cons_name, [missing_file])

    # Le producteur doit être replanifié avec outputs_unfetchable_from_peers
    assert res["success"] is True
    assert res.get("retriggered_node") == prod_name
    assert res.get("reason") == "outputs_unfetchable_from_peers"

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, stale_reason, missing_deps_retried FROM job_nodes WHERE job_id = ? AND node_name = ?", (job_id, prod_name))
        row = cursor.fetchone()
        assert row["status"] == "ready"
        assert row["stale_reason"] == "outputs_unfetchable_from_peers"
        assert row["missing_deps_retried"] == 1

