"""
Tests unitaires pour le module db_retention.py (Cluster-CI v3 - Règle A15).
"""

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import sqlite3
import time
import pytest

from src.scheduler.db_retention import (
    RetentionConfig,
    RetentionReport,
    run_retention,
    run_retention_periodic,
    select_candidate_jobs,
    purge_jobs_from_database,
)


@pytest.fixture
def test_db(tmp_path):
    """Prépare une base de données SQLite temporaire avec schéma complet (v3 inclus)."""
    db_file = tmp_path / "test_scheduler.db"

    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE workers (
            worker_id TEXT PRIMARY KEY,
            hostname TEXT,
            status TEXT DEFAULT 'online',
            assigned_job_id TEXT
        );
    """)

    cursor.execute("""
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            repo TEXT,
            branch TEXT,
            status TEXT DEFAULT 'pending',
            worker_id TEXT,
            created_at TIMESTAMP,
            finished_at TIMESTAMP,
            FOREIGN KEY (worker_id) REFERENCES workers (worker_id)
        );
    """)

    cursor.execute("""
        CREATE TABLE job_nodes (
            job_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            worker_id TEXT,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            PRIMARY KEY (job_id, node_name),
            FOREIGN KEY (job_id) REFERENCES jobs (job_id)
        );
    """)

    cursor.execute("""
        CREATE TABLE runner_heartbeats (
            job_id TEXT NOT NULL,
            runner_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (job_id, runner_id)
        );
    """)

    cursor.execute("""
        CREATE TABLE node_artifacts (
            job_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            md5 TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            worker_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (job_id, node_name, md5, worker_id)
        );
    """)

    conn.commit()
    yield conn, db_file
    conn.close()


def test_nominal_database_is_never_purged(test_db):
    """Règle A15 : Une base de taille normale (< 1 Go) n'est JAMAIS purgée, tout l'historique est conservé."""
    conn, db_file = test_db
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    old_date = (now - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")

    # Même des jobs très anciens (> 400 jours) sont conservés si la base n'est pas pathologique
    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('j_old_done', 'completed', ?, ?)", (old_date, old_date))
    conn.commit()

    config = RetentionConfig(pathological_threshold_bytes=1024 * 1024 * 1024, force=False)
    report = run_retention(conn=conn, now=now, config=config)

    assert report.pathological_growth_detected is False
    assert report.jobs_purged == 0

    cursor.execute("SELECT count(*) FROM jobs WHERE job_id = 'j_old_done'")
    assert cursor.fetchone()[0] == 1


def test_pathological_growth_purges_only_jobs_older_than_365_days(test_db):
    """Règle A15 : En cas de dépassement pathologique, seuls les jobs terminés > 365j sont purgés."""
    conn, db_file = test_db
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    date_200d_ago = (now - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    date_400d_ago = (now - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('j_recent_done', 'completed', ?, ?)", (date_200d_ago, date_200d_ago))
    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('j_very_old_done', 'failed', ?, ?)", (date_400d_ago, date_400d_ago))
    conn.commit()

    # Forcer la purge pathologique (ou threshold=0)
    config = RetentionConfig(retention_days=365, force=True)
    report = run_retention(conn=conn, now=now, config=config)

    assert report.jobs_purged == 1
    assert report.rows_deleted["jobs"] == 1

    # Le job de 200 jours est toujours présent
    cursor.execute("SELECT count(*) FROM jobs WHERE job_id = 'j_recent_done'")
    assert cursor.fetchone()[0] == 1

    # Le job de 400 jours est purgé
    cursor.execute("SELECT count(*) FROM jobs WHERE job_id = 'j_very_old_done'")
    assert cursor.fetchone()[0] == 0


def test_active_jobs_are_never_purged_even_if_ancient(test_db):
    """Règle A15 : Même en cas de dépassement pathologique, AUCUN job actif n'est JAMAIS purgé."""
    conn, db_file = test_db
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    ancient_date = (now - timedelta(days=500)).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('j_running_ancient', 'running', ?, NULL)", (ancient_date,))
    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('j_pending_ancient', 'pending', ?, NULL)", (ancient_date,))
    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('j_completed_ancient', 'completed', ?, ?)", (ancient_date, ancient_date))
    conn.commit()

    config = RetentionConfig(retention_days=365, force=True)
    report = run_retention(conn=conn, now=now, config=config)

    assert report.jobs_purged == 1
    assert report.active_jobs_preserved == 2

    # Vérification dans la table
    cursor.execute("SELECT status FROM jobs WHERE job_id = 'j_running_ancient'")
    assert cursor.fetchone()[0] == "running"
    cursor.execute("SELECT status FROM jobs WHERE job_id = 'j_pending_ancient'")
    assert cursor.fetchone()[0] == "pending"
    cursor.execute("SELECT count(*) FROM jobs WHERE job_id = 'j_completed_ancient'")
    assert cursor.fetchone()[0] == 0


def test_full_cascade_database_purge(test_db):
    """Vérifie la suppression en cascade exhaustive : job_nodes, runner_heartbeats, node_artifacts, workers."""
    conn, db_file = test_db
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    ancient_date = (now - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("INSERT INTO workers (worker_id, hostname, assigned_job_id) VALUES ('w1', 'worker-1', 'job_to_purge')")
    cursor.execute("INSERT INTO jobs (job_id, status, worker_id, created_at, finished_at) VALUES ('job_to_purge', 'completed', 'w1', ?, ?)", (ancient_date, ancient_date))
    cursor.execute("INSERT INTO job_nodes (job_id, node_name, status) VALUES ('job_to_purge', 'train', 'done')")
    cursor.execute("INSERT INTO runner_heartbeats (job_id, runner_id, worker_id) VALUES ('job_to_purge', 'r1', 'w1')")
    cursor.execute("INSERT INTO node_artifacts (job_id, node_name, md5, size_bytes, worker_id) VALUES ('job_to_purge', 'train', 'md5hash', 1024, 'w1')")
    conn.commit()

    config = RetentionConfig(retention_days=365, force=True)
    report = run_retention(conn=conn, now=now, config=config)

    assert report.rows_deleted["jobs"] == 1
    assert report.rows_deleted["job_nodes"] == 1
    assert report.rows_deleted["runner_heartbeats"] == 1
    assert report.rows_deleted["node_artifacts"] == 1
    assert report.rows_deleted["workers_disassociated"] == 1

    cursor.execute("SELECT count(*) FROM jobs WHERE job_id = 'job_to_purge'")
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT count(*) FROM job_nodes WHERE job_id = 'job_to_purge'")
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT count(*) FROM runner_heartbeats WHERE job_id = 'job_to_purge'")
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT count(*) FROM node_artifacts WHERE job_id = 'job_to_purge'")
    assert cursor.fetchone()[0] == 0

    # Worker préservé mais assigned_job_id délié
    cursor.execute("SELECT assigned_job_id FROM workers WHERE worker_id = 'w1'")
    assert cursor.fetchone()[0] is None


def test_dry_run_leaves_database_intact(test_db):
    """Le mode dry-run ne modifie rien."""
    conn, db_file = test_db
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    ancient_date = (now - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('dry_job', 'completed', ?, ?)", (ancient_date, ancient_date))
    conn.commit()

    config = RetentionConfig(retention_days=365, force=True, dry_run=True)
    report = run_retention(conn=conn, now=now, config=config)

    assert report.dry_run is True
    assert report.jobs_purged == 1
    assert report.rows_deleted["jobs"] == 1

    cursor.execute("SELECT count(*) FROM jobs WHERE job_id = 'dry_job'")
    assert cursor.fetchone()[0] == 1


def test_run_retention_periodic_interval(test_db):
    """Vérifie le respect du cadencement de run_retention_periodic."""
    conn, db_file = test_db
    config = RetentionConfig(force=False)

    t1, report1 = run_retention_periodic(last_run_timestamp=time.time(), interval_seconds=3600, config=config, db_path=str(db_file))
    assert report1 is None

    t2, report2 = run_retention_periodic(last_run_timestamp=0.0, interval_seconds=3600, config=config, db_path=str(db_file))
    assert report2 is not None
    assert t2 > 0
