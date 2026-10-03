"""
Tests unitaires pour le module db_retention.py (Cluster-CI v3).
"""

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import pytest

from src.scheduler.db_retention import (
    RetentionConfig,
    RetentionReport,
    run_retention,
    run_retention_periodic,
    select_candidate_jobs,
    purge_jobs_from_database,
    purge_associated_disk_files,
    truncate_large_job_logs,
)


@pytest.fixture
def test_db_and_logs(tmp_path):
    """Prépare une base de données temporaire complète avec le schéma v3 et un dossier de logs."""
    db_file = tmp_path / "test_scheduler.db"
    log_dir = tmp_path / "job_logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    cursor = conn.cursor()

    # Schéma complet incluant tables existantes et v3
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
            local_archive_path TEXT,
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
    yield conn, db_file, log_dir
    conn.close()


def test_active_jobs_are_never_purged(test_db_and_logs):
    """Règle d'or : AUCUN job actif (running/pending/assigned) n'est jamais purgé, même très ancien."""
    conn, db_file, log_dir = test_db_and_logs
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    old_date = (now - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")

    # 1 job ancien running, 1 job ancien pending, 1 job ancien completed
    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('job_old_running', 'running', ?, NULL)", (old_date,))
    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('job_old_pending', 'pending', ?, NULL)", (old_date,))
    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('job_old_completed', 'completed', ?, ?)", (old_date, old_date))
    conn.commit()

    config = RetentionConfig(retention_days=30, min_retained_jobs=0, max_retained_jobs=1000)
    candidates, total_analyzed, active_preserved = select_candidate_jobs(conn, now, config)

    candidate_ids = [c["job_id"] for c in candidates]
    assert "job_old_completed" in candidate_ids
    assert "job_old_running" not in candidate_ids
    assert "job_old_pending" not in candidate_ids
    assert active_preserved == 2
    assert total_analyzed == 3


def test_retention_by_age_and_cutoff(test_db_and_logs):
    """Les jobs terminés plus récents que retention_days sont conservés, les plus anciens sont purgés."""
    conn, db_file, log_dir = test_db_and_logs
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    date_10d_ago = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    date_40d_ago = (now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('job_recent', 'completed', ?, ?)", (date_10d_ago, date_10d_ago))
    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('job_old', 'failed', ?, ?)", (date_40d_ago, date_40d_ago))
    conn.commit()

    config = RetentionConfig(retention_days=30, min_retained_jobs=0)
    candidates, _, _ = select_candidate_jobs(conn, now, config)
    candidate_ids = [c["job_id"] for c in candidates]

    assert candidate_ids == ["job_old"]


def test_max_retained_jobs_ceiling(test_db_and_logs):
    """Le plafond secondaire max_retained_jobs purge les jobs terminés excédentaires les plus anciens."""
    conn, db_file, log_dir = test_db_and_logs
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)

    # Création de 10 jobs terminés récents (âgés de 1 à 10 jours)
    for i in range(1, 11):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES (?, 'completed', ?, ?)", (f"job_{i:02d}", d, d))
    conn.commit()

    # Configuration avec retention_days=30 (aucun n'a >30j), mais max_retained_jobs=4 et min_retained_jobs=0
    config = RetentionConfig(retention_days=30, max_retained_jobs=4, min_retained_jobs=0)
    candidates, _, _ = select_candidate_jobs(conn, now, config)

    # 10 jobs terminés au total, plafond 4 => 6 jobs les plus anciens doivent être purgés (job_10 à job_05)
    assert len(candidates) == 6
    purged_ids = {c["job_id"] for c in candidates}
    assert "job_10" in purged_ids
    assert "job_09" in purged_ids
    assert "job_01" not in purged_ids
    assert "job_02" not in purged_ids


def test_min_retained_jobs_safety_floor(test_db_and_logs):
    """Le plancher de sécurité min_retained_jobs empêche de vider la base même si les jobs sont vieux."""
    conn, db_file, log_dir = test_db_and_logs
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    old_date = (now - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")

    # 3 jobs vieux
    for i in range(3):
        cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES (?, 'completed', ?, ?)", (f"job_old_{i}", old_date, old_date))
    conn.commit()

    # min_retained_jobs=5 > 3 jobs présents
    config = RetentionConfig(retention_days=30, min_retained_jobs=5)
    candidates, _, _ = select_candidate_jobs(conn, now, config)
    assert len(candidates) == 0


def test_full_cascade_database_purge(test_db_and_logs):
    """Vérifie la suppression en cascade exhaustive : job_nodes, runner_heartbeats, node_artifacts, workers."""
    conn, db_file, log_dir = test_db_and_logs
    cursor = conn.cursor()

    # Configuration worker et job
    cursor.execute("INSERT INTO workers (worker_id, hostname, assigned_job_id) VALUES ('w1', 'worker-1', 'job_to_purge')")
    cursor.execute("INSERT INTO jobs (job_id, status, worker_id) VALUES ('job_to_purge', 'completed', 'w1')")
    cursor.execute("INSERT INTO job_nodes (job_id, node_name, status) VALUES ('job_to_purge', 'train', 'done')")
    cursor.execute("INSERT INTO runner_heartbeats (job_id, runner_id, worker_id) VALUES ('job_to_purge', 'r1', 'w1')")
    cursor.execute("INSERT INTO node_artifacts (job_id, node_name, md5, size_bytes, worker_id) VALUES ('job_to_purge', 'train', 'md5hash', 1024, 'w1')")
    conn.commit()

    # Purge
    deleted = purge_jobs_from_database(conn, ["job_to_purge"], dry_run=False)

    assert deleted["jobs"] == 1
    assert deleted["job_nodes"] == 1
    assert deleted["runner_heartbeats"] == 1
    assert deleted["node_artifacts"] == 1
    assert deleted["workers_disassociated"] == 1

    # Vérifications dans les tables
    cursor.execute("SELECT count(*) FROM jobs WHERE job_id = 'job_to_purge'")
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT count(*) FROM job_nodes WHERE job_id = 'job_to_purge'")
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT count(*) FROM runner_heartbeats WHERE job_id = 'job_to_purge'")
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT count(*) FROM node_artifacts WHERE job_id = 'job_to_purge'")
    assert cursor.fetchone()[0] == 0

    # Worker existe toujours mais assigned_job_id est NULL
    cursor.execute("SELECT assigned_job_id FROM workers WHERE worker_id = 'w1'")
    assert cursor.fetchone()[0] is None


def test_purge_associated_disk_files(test_db_and_logs):
    """Vérifie la suppression des fichiers log et archives associés avec comptage des octets."""
    conn, db_file, log_dir = test_db_and_logs

    # Création d'un fichier log
    log_file = log_dir / "job_disk.log"
    log_content = b"x" * 2048
    log_file.write_bytes(log_content)

    # Création d'une fausse archive
    archive_file = log_dir.parent / "fake_archive.tar.gz"
    archive_file.write_bytes(b"y" * 4096)

    candidates = [{"job_id": "job_disk", "local_archive_path": str(archive_file)}]

    # Dry run
    files_del_dry, bytes_dry = purge_associated_disk_files(candidates, log_dir, dry_run=True)
    assert files_del_dry == 2
    assert bytes_dry == 2048 + 4096
    assert log_file.exists()
    assert archive_file.exists()

    # Réel
    files_del, bytes_freed = purge_associated_disk_files(candidates, log_dir, dry_run=False)
    assert files_del == 2
    assert bytes_freed == 2048 + 4096
    assert not log_file.exists()
    assert not archive_file.exists()


def test_truncate_large_job_logs(test_db_and_logs):
    """Vérifie la troncature intelligente des logs individuels dépassant le plafond."""
    conn, db_file, log_dir = test_db_and_logs

    big_log = log_dir / "giant_job.log"
    # Créer un fichier de 5 Mo (max fixé à 2 Mo pour le test)
    total_size = 5 * 1024 * 1024
    big_log.write_bytes(b"A" * (1024 * 1024) + b"B" * (3 * 1024 * 1024) + b"Z" * (1024 * 1024))

    # max_bytes = 2 Mo
    trunc_count, bytes_freed = truncate_large_job_logs(log_dir, max_log_bytes=2 * 1024 * 1024, dry_run=False)
    assert trunc_count == 1
    assert bytes_freed > 0

    new_size = big_log.stat().st_size
    assert new_size < total_size
    content = big_log.read_bytes()
    assert b"TRUNCATED" in content
    assert content.startswith(b"A" * 100)
    assert content.endswith(b"Z" * 100)


def test_dry_run_leaves_database_and_disk_intact(test_db_and_logs):
    """Le mode dry-run ne modifie absolument rien mais calcule fidèlement le rapport."""
    conn, db_file, log_dir = test_db_and_logs
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    old_date = (now - timedelta(days=50)).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES ('dry_job', 'completed', ?, ?)", (old_date, old_date))
    cursor.execute("INSERT INTO job_nodes (job_id, node_name, status) VALUES ('dry_job', 'n1', 'done')")
    conn.commit()

    log_file = log_dir / "dry_job.log"
    log_file.write_bytes(b"hello world")

    config = RetentionConfig(retention_days=30, min_retained_jobs=0, dry_run=True, log_dir=str(log_dir))
    report = run_retention(conn=conn, now=now, config=config)

    assert report.dry_run is True
    assert report.jobs_purged == 1
    assert report.rows_deleted["jobs"] == 1
    assert report.rows_deleted["job_nodes"] == 1
    assert report.files_deleted == 1

    # Vérification d'intégrité : la ligne et le fichier existent toujours
    cursor.execute("SELECT count(*) FROM jobs WHERE job_id = 'dry_job'")
    assert cursor.fetchone()[0] == 1
    assert log_file.exists()


def test_sqlite_integrity_and_vacuum(test_db_and_logs):
    """Vérifie l'exécution du compactage et le maintien parfait de PRAGMA integrity_check."""
    conn, db_file, log_dir = test_db_and_logs
    cursor = conn.cursor()

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    old_date = (now - timedelta(days=50)).strftime("%Y-%m-%d %H:%M:%S")

    for i in range(50):
        cursor.execute("INSERT INTO jobs (job_id, status, created_at, finished_at) VALUES (?, 'completed', ?, ?)", (f"j_{i}", old_date, old_date))
    conn.commit()

    config = RetentionConfig(retention_days=30, min_retained_jobs=0, dry_run=False, vacuum_mode="full", log_dir=str(log_dir))
    report = run_retention(conn=conn, now=now, config=config)

    assert report.jobs_purged == 50

    cursor.execute("PRAGMA integrity_check;")
    check = cursor.fetchone()[0]
    assert check == "ok"


def test_run_retention_periodic_interval(test_db_and_logs):
    """Vérifie que run_retention_periodic respecte le cadencement."""
    conn, db_file, log_dir = test_db_and_logs
    config = RetentionConfig(min_retained_jobs=0, log_dir=str(log_dir))

    last_run = 1000.0
    # Si temps actuel < last_run + 3600 -> pas d'exécution
    t1, report1 = run_retention_periodic(last_run_timestamp=time.time(), interval_seconds=3600, config=config, conn=conn)
    assert report1 is None

    # Si last_run = 0 (délai dépassé) -> exécution
    t2, report2 = run_retention_periodic(last_run_timestamp=0.0, interval_seconds=3600, config=config, conn=conn)
    assert report2 is not None
    assert t2 > 0
