"""
test_migration_idempotence.py - Validation de la migration SQLite et son idempotence.
Prouve que :
1. Une base existante avec l'ancien schéma de 0358518 (sans attempt, retry_count, failure_reason, cas_transfers)
   est migrée avec succès par init_db().
2. Toutes les données préexistantes sont strictement préservées.
3. Les nouvelles colonnes sont créées avec leurs valeurs par défaut.
4. Des appels répétés à init_db() sont rigoureusement idempotents (zéro crash).
"""

import sqlite3

from src.scheduler.persistence import init_db, get_db_conn


def test_sqlite_migration_on_preexisting_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "legacy_headnode_cluster.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", db_file)

    # 1. Création manuelle d'une base selon le schéma historique (commit 0358518)
    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                repo TEXT,
                branch TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                duration_s REAL,
                exit_code INTEGER,
                runner_id TEXT,
                worker_id TEXT,
                error_message TEXT,
                commit_hash TEXT,
                gh_token TEXT,
                parallel_mode INTEGER DEFAULT 0,
                executor_role TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE job_nodes (
                job_id TEXT NOT NULL,
                node_name TEXT NOT NULL,
                status TEXT NOT NULL,
                worker_id TEXT,
                runner_id TEXT,
                image TEXT,
                priority INTEGER DEFAULT 0,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                duration_s REAL,
                exit_code INTEGER,
                error_message TEXT,
                missing_deps_retried INTEGER DEFAULT 0,
                gpu_ids TEXT DEFAULT '[]',
                PRIMARY KEY (job_id, node_name),
                FOREIGN KEY (job_id) REFERENCES jobs (job_id)
            )
        ''')

        # 2. Insertion de données existantes représentatives de la production
        cursor.execute("INSERT INTO jobs (job_id, status, repo, branch, parallel_mode) VALUES ('job-legacy-001', 'completed', 'test/repo', 'main', 1)")
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, worker_id, runner_id, image, duration_s, exit_code, missing_deps_retried)
            VALUES ('job-legacy-001', 'prep', 'done', 'worker-legacy', 'runner-legacy', 'python:3.11', 42.5, 0, 0)
        ''')
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, worker_id, runner_id, image, duration_s, exit_code, missing_deps_retried)
            VALUES ('job-legacy-001', 'train', 'done', 'worker-legacy', 'runner-legacy', 'python:3.12', 123.4, 0, 1)
        ''')
        conn.commit()

        # Vérifier que les colonnes du Bug 12 sont absentes initialement
        cursor.execute("PRAGMA table_info(job_nodes)")
        initial_cols = [row[1] for row in cursor.fetchall()]
        assert "attempt" not in initial_cols
        assert "retry_count" not in initial_cols
        assert "failure_reason" not in initial_cols
        assert "cas_transfers" not in initial_cols

    # 3. Exécution de la migration via init_db()
    init_db()

    # 4. Vérification que la migration a ajouté les colonnes et conservé les données
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(job_nodes)")
        migrated_cols = {row[1]: row for row in cursor.fetchall()}

        for expected_col in ["attempt", "retry_count", "failure_reason", "cas_transfers"]:
            assert expected_col in migrated_cols, f"La colonne {expected_col} doit être présente après migration"

        # Vérification de l'intégrité absolue des données préexistantes
        cursor.execute("SELECT node_name, status, worker_id, duration_s, exit_code, attempt, retry_count, failure_reason, cas_transfers FROM job_nodes WHERE job_id = 'job-legacy-001' ORDER BY node_name")
        rows = cursor.fetchall()
        assert len(rows) == 2

        prep_row = dict(rows[0])
        assert prep_row["node_name"] == "prep"
        assert prep_row["status"] == "done"
        assert prep_row["worker_id"] == "worker-legacy"
        assert prep_row["duration_s"] == 42.5
        assert prep_row["exit_code"] == 0
        assert prep_row["attempt"] == 0
        assert prep_row["retry_count"] == 0
        assert prep_row["failure_reason"] is None
        assert prep_row["cas_transfers"] == "[]"

        train_row = dict(rows[1])
        assert train_row["node_name"] == "train"
        assert train_row["status"] == "done"
        assert train_row["duration_s"] == 123.4
        assert train_row["attempt"] == 0
        assert train_row["retry_count"] == 0

    # 5. Preuve d'idempotence : ré-exécution d'init_db() sans erreur
    init_db()
    init_db()

    # Les données sont toujours intactes après multiples migrations
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM job_nodes WHERE job_id = 'job-legacy-001'")
        assert cursor.fetchone()[0] == 2
