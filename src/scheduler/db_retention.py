"""
Module de surveillance et rétention pathologique de la base SQLite (Cluster-CI v3 - Règle A15).

Règles de rétention (Amendement A15) :
- La base normale (actuellement ~15 Mo pour 3 017 jobs) n'est JAMAIS purgée : l'historique complet est conservé.
- Seule la croissance pathologique est bornée : si la base dépasse 1 Go (seuil dans src/config/defaults.py),
  les jobs terminés de plus de 365 jours sont purgés par lots de transactions courtes.
- JAMAIS de purge des jobs actifs (pending, running, assigned, queued).
- JAMAIS de VACUUM FULL bloquant le scheduler : utilisation exclusive de PRAGMA incremental_vacuum et checkpoint WAL.
- Les fichiers de logs de jobs (job_logs/{job_id}.log) sont INTÉGRALEMENT conservés (pas de suppression ni troncature).
- Gestion de cascade exhaustive sur les tables v3 (job_nodes, runner_heartbeats, node_artifacts, workers).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping, Sequence

logger = logging.getLogger("cluster_ci.db_retention")

# Constantes par défaut de la règle A15
try:
    from src.config.defaults import (
        DB_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES,
        DB_RETENTION_PATHOLOGICAL_DAYS,
    )
except ImportError:
    try:
        from config.defaults import (
            DB_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES,
            DB_RETENTION_PATHOLOGICAL_DAYS,
        )
    except ImportError:
        DB_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES = 1024 * 1024 * 1024  # 1 Go
        DB_RETENTION_PATHOLOGICAL_DAYS = 365  # 365 jours

# Statuts protégés : un job dans l'un de ces statuts n'est JAMAIS purgé
ACTIVE_JOB_STATUSES = frozenset({"pending", "running", "assigned", "queued"})

# Statuts terminaux éligibles à la purge en cas de dépassement pathologique
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "stopped"})


@dataclass
class RetentionConfig:
    """Configuration de la politique de rétention A15."""
    pathological_threshold_bytes: int = DB_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES  # 1 Go
    retention_days: int = DB_RETENTION_PATHOLOGICAL_DAYS  # 365 jours
    batch_size: int = 100
    incremental_vacuum_pages: int = 1000
    force: bool = False  # Si True, ignore le seuil pathologique (utilisé pour bancs d'essai et tests)
    dry_run: bool = False

    @classmethod
    def from_env(cls, **overrides) -> RetentionConfig:
        """Instancie la configuration depuis l'environnement avec surcharge."""
        threshold = int(os.environ.get("CLUSTER_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES", str(DB_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES)))
        retention_days = int(os.environ.get("CLUSTER_RETENTION_DAYS", str(DB_RETENTION_PATHOLOGICAL_DAYS)))
        batch_size = int(os.environ.get("CLUSTER_RETENTION_BATCH_SIZE", "100"))
        dry_run = os.environ.get("CLUSTER_RETENTION_DRY_RUN", "0") in ("1", "true", "True")
        force = os.environ.get("CLUSTER_RETENTION_FORCE", "0") in ("1", "true", "True")

        cfg_dict: dict[str, Any] = {
            "pathological_threshold_bytes": threshold,
            "retention_days": retention_days,
            "batch_size": batch_size,
            "dry_run": dry_run,
            "force": force,
        }
        cfg_dict.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**cfg_dict)


@dataclass
class RetentionReport:
    """Rapport d'exécution de la surveillance et rétention A15."""
    dry_run: bool = False
    timestamp: str = ""
    duration_s: float = 0.0
    db_size_bytes: int = 0
    pathological_threshold_bytes: int = 0
    pathological_growth_detected: bool = False
    jobs_analyzed: int = 0
    active_jobs_preserved: int = 0
    terminal_jobs_total: int = 0
    jobs_purged: int = 0
    rows_deleted: dict[str, int] = field(default_factory=dict)
    incremental_vacuum_performed: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "timestamp": self.timestamp,
            "duration_s": round(self.duration_s, 3),
            "db_size_bytes": self.db_size_bytes,
            "pathological_threshold_bytes": self.pathological_threshold_bytes,
            "pathological_growth_detected": self.pathological_growth_detected,
            "jobs_analyzed": self.jobs_analyzed,
            "active_jobs_preserved": self.active_jobs_preserved,
            "terminal_jobs_total": self.terminal_jobs_total,
            "jobs_purged": self.jobs_purged,
            "rows_deleted": self.rows_deleted,
            "incremental_vacuum_performed": self.incremental_vacuum_performed,
            "errors": self.errors,
        }


def get_existing_tables(conn: sqlite3.Connection) -> set[str]:
    """Retourne la liste des tables présentes dans la base SQLite."""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    return {row[0] for row in cursor.fetchall()}


def parse_sqlite_timestamp(ts_val: Any) -> datetime | None:
    """Convertit un horodatage textuel SQLite en datetime UTC."""
    if not ts_val:
        return None
    if isinstance(ts_val, datetime):
        if ts_val.tzinfo is None:
            return ts_val.replace(tzinfo=timezone.utc)
        return ts_val.astimezone(timezone.utc)
    ts_str = str(ts_val).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def select_candidate_jobs(
    conn: sqlite3.Connection,
    now: datetime,
    config: RetentionConfig,
) -> tuple[list[dict[str, Any]], int, int]:
    """
    Sélectionne les jobs terminés éligibles à la purge pathologique.
    
    Invariants A15 :
    - AUCUN job actif (pending, running, assigned, queued) n'est sélectionné.
    - Seuls les jobs terminés > retention_days (défaut 365j) sont éligibles.
    """
    cursor = conn.cursor()

    # 1. Dénombrement strict des jobs actifs protégés
    cursor.execute("""
        SELECT count(*) FROM jobs 
        WHERE status IN ('pending', 'running', 'assigned', 'queued')
    """)
    active_count = cursor.fetchone()[0]

    # 2. Récupération des jobs terminés
    cursor.execute("""
        SELECT job_id, status, created_at, finished_at 
        FROM jobs 
        WHERE status NOT IN ('pending', 'running', 'assigned', 'queued')
        ORDER BY COALESCE(finished_at, created_at) ASC
    """)
    rows = cursor.fetchall()
    terminal_jobs = [
        {
            "job_id": r[0],
            "status": r[1],
            "created_at": r[2],
            "finished_at": r[3],
        }
        for r in rows
    ]

    total_terminal = len(terminal_jobs)
    total_analyzed = active_count + total_terminal

    cutoff_date = now - timedelta(days=config.retention_days)
    candidates: list[dict[str, Any]] = []

    for job in terminal_jobs:
        dt_ref = parse_sqlite_timestamp(job["finished_at"]) or parse_sqlite_timestamp(job["created_at"])
        if dt_ref and dt_ref < cutoff_date:
            # Garde-fou d'intégrité absolu anti-purge active
            if job["status"] in ACTIVE_JOB_STATUSES:
                logger.error(f"CRITICAL: Job {job['job_id']} has active status {job['status']}. Skipping!")
                continue
            candidates.append(job)

    return candidates, total_analyzed, active_count


def purge_jobs_from_database(
    conn: sqlite3.Connection,
    job_ids: Sequence[str],
    dry_run: bool = False,
    batch_size: int = 100,
) -> dict[str, int]:
    """
    Supprime les jobs et leurs enregistrements liés dans les tables v3
    par transactions courtes par lots de batch_size (défaut 100) pour ne pas verrouiller le WAL.
    
    Ordre de dépendance :
    1. runner_heartbeats
    2. node_artifacts
    3. job_nodes
    4. workers (désassociation de assigned_job_id)
    5. jobs
    """
    rows_deleted: dict[str, int] = {
        "runner_heartbeats": 0,
        "node_artifacts": 0,
        "job_nodes": 0,
        "workers_disassociated": 0,
        "jobs": 0,
    }

    if not job_ids:
        return rows_deleted

    existing_tables = get_existing_tables(conn)
    cursor = conn.cursor()

    for i in range(0, len(job_ids), batch_size):
        chunk = job_ids[i : i + batch_size]
        placeholders = ",".join("?" for _ in chunk)

        if dry_run:
            if "runner_heartbeats" in existing_tables:
                cursor.execute(f"SELECT count(*) FROM runner_heartbeats WHERE job_id IN ({placeholders})", chunk)
                rows_deleted["runner_heartbeats"] += cursor.fetchone()[0]

            if "node_artifacts" in existing_tables:
                cursor.execute(f"SELECT count(*) FROM node_artifacts WHERE job_id IN ({placeholders})", chunk)
                rows_deleted["node_artifacts"] += cursor.fetchone()[0]

            if "job_nodes" in existing_tables:
                cursor.execute(f"SELECT count(*) FROM job_nodes WHERE job_id IN ({placeholders})", chunk)
                rows_deleted["job_nodes"] += cursor.fetchone()[0]

            if "workers" in existing_tables:
                cursor.execute("PRAGMA table_info(workers)")
                col_names = {c[1] for c in cursor.fetchall()}
                if "assigned_job_id" in col_names:
                    cursor.execute(f"SELECT count(*) FROM workers WHERE assigned_job_id IN ({placeholders})", chunk)
                    rows_deleted["workers_disassociated"] += cursor.fetchone()[0]

            cursor.execute(f"SELECT count(*) FROM jobs WHERE job_id IN ({placeholders})", chunk)
            rows_deleted["jobs"] += cursor.fetchone()[0]

        else:
            try:
                conn.execute("BEGIN TRANSACTION")

                if "runner_heartbeats" in existing_tables:
                    cursor.execute(f"DELETE FROM runner_heartbeats WHERE job_id IN ({placeholders})", chunk)
                    rows_deleted["runner_heartbeats"] += cursor.rowcount

                if "node_artifacts" in existing_tables:
                    cursor.execute(f"DELETE FROM node_artifacts WHERE job_id IN ({placeholders})", chunk)
                    rows_deleted["node_artifacts"] += cursor.rowcount

                if "job_nodes" in existing_tables:
                    cursor.execute(f"DELETE FROM job_nodes WHERE job_id IN ({placeholders})", chunk)
                    rows_deleted["job_nodes"] += cursor.rowcount

                if "workers" in existing_tables:
                    cursor.execute("PRAGMA table_info(workers)")
                    col_names = {c[1] for c in cursor.fetchall()}
                    if "assigned_job_id" in col_names:
                        cursor.execute(
                            f"UPDATE workers SET assigned_job_id = NULL WHERE assigned_job_id IN ({placeholders})",
                            chunk,
                        )
                        rows_deleted["workers_disassociated"] += cursor.rowcount

                cursor.execute(f"DELETE FROM jobs WHERE job_id IN ({placeholders})", chunk)
                rows_deleted["jobs"] += cursor.rowcount

                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"Error purging batch [{i}:{i+len(chunk)}]: {e}")
                raise

    return rows_deleted


def run_incremental_vacuum_if_supported(conn: sqlite3.Connection, pages: int = 1000) -> bool:
    """
    Exécute PRAGMA incremental_vacuum sans verrou bloquant prolongé si auto_vacuum=2.
    Effectue également un wal_checkpoint pour compacter le journal WAL sans bloquer.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA auto_vacuum;")
    row = cursor.fetchone()
    auto_vac = row[0] if row else 0

    vacuum_performed = False
    if auto_vac == 2:  # INCREMENTAL
        cursor.execute(f"PRAGMA incremental_vacuum({pages});")
        vacuum_performed = True

    try:
        cursor.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except Exception as e:
        logger.debug(f"wal_checkpoint PASSIVE: {e}")

    return vacuum_performed


def run_retention(
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
    config: RetentionConfig | None = None,
    db_path: str | Path | None = None,
) -> RetentionReport:
    """
    Point d'entrée principal de rétention pathologique (Règle A15).

    Comportement :
    1. Mesure la taille réelle de la base SQLite.
    2. Si taille <= seuil pathologique (1 Go) et non forcé :
       -> Conserve 100% de la base, aucune purge effectuée.
    3. Si taille > seuil pathologique (1 Go) ou forcé :
       -> Purge par lots les jobs terminés > 365 jours.
       -> Zéro suppression des logs de jobs (conservés).
       -> Pas de VACUUM FULL bloquant.
    """
    t_start = time.time()
    now_utc = now or datetime.now(timezone.utc)
    cfg = config or RetentionConfig.from_env()

    report = RetentionReport(
        dry_run=cfg.dry_run,
        timestamp=now_utc.isoformat(),
        pathological_threshold_bytes=cfg.pathological_threshold_bytes,
    )

    should_close_conn = False
    resolved_db_path: Path | None = None

    if conn is None:
        raw_db_path = db_path or os.environ.get("CLUSTER_DB_PATH", "cluster_scheduler.db")
        resolved_db_path = Path(raw_db_path).resolve()
        if not resolved_db_path.exists():
            report.errors.append(f"Database file not found: {resolved_db_path}")
            report.duration_s = time.time() - t_start
            return report
        conn = sqlite3.connect(str(resolved_db_path), timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        should_close_conn = True
    else:
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA database_list;")
            for row in cursor.fetchall():
                if row[1] == "main" and row[2]:
                    resolved_db_path = Path(row[2])
                    break
        except Exception as e:
            logger.debug(f"Could not resolve db file path from connection: {e}")

    # Mesure de la taille du fichier DB
    db_size = 0
    if resolved_db_path and resolved_db_path.is_file():
        db_size = resolved_db_path.stat().st_size
    report.db_size_bytes = db_size

    # Vérification du seuil pathologique A15 (> 1 Go)
    is_pathological = (db_size > cfg.pathological_threshold_bytes)
    report.pathological_growth_detected = is_pathological

    if not is_pathological and not cfg.force:
        # Base de taille normale : l'historique complet est préservé
        logger.info(
            f"Database size ({db_size / (1024*1024):.2f} Mo) is below pathological threshold "
            f"({cfg.pathological_threshold_bytes / (1024*1024*1024):.1f} Go). "
            "Full job history is preserved (Rule A15)."
        )
        if should_close_conn:
            conn.close()
        report.duration_s = time.time() - t_start
        return report

    logger.warning(
        f"Pathological growth detected (DB size: {db_size / (1024*1024):.2f} Mo > "
        f"{cfg.pathological_threshold_bytes / (1024*1024):.2f} Mo) or forced. "
        f"Purging terminal jobs older than {cfg.retention_days} days."
    )

    try:
        # 1. Sélection des candidats terminés > retention_days
        candidates, total_analyzed, active_preserved = select_candidate_jobs(conn, now_utc, cfg)
        report.jobs_analyzed = total_analyzed
        report.active_jobs_preserved = active_preserved
        report.terminal_jobs_total = total_analyzed - active_preserved
        report.jobs_purged = len(candidates)

        candidate_ids = [c["job_id"] for c in candidates]

        # 2. Purge ordonnée en cascade dans la base SQLite par transactions courtes
        rows_deleted = purge_jobs_from_database(
            conn=conn,
            job_ids=candidate_ids,
            dry_run=cfg.dry_run,
            batch_size=cfg.batch_size,
        )
        report.rows_deleted = rows_deleted

        # 3. Récupération d'espace disque non bloquante (incremental_vacuum si supporté)
        if not cfg.dry_run and len(candidate_ids) > 0:
            vac_ok = run_incremental_vacuum_if_supported(conn, pages=cfg.incremental_vacuum_pages)
            report.incremental_vacuum_performed = vac_ok

    except Exception as e:
        report.errors.append(str(e))
        logger.error(f"Retention execution error: {e}", exc_info=True)
    finally:
        if should_close_conn:
            conn.close()

    report.duration_s = time.time() - t_start
    return report


def run_retention_periodic(
    last_run_timestamp: float,
    interval_seconds: float = 86400.0,
    config: RetentionConfig | None = None,
    db_path: str | Path | None = None,
) -> tuple[float, RetentionReport | None]:
    """
    Helper pour exécution cadencée dans la boucle du scheduler (1x/jour).
    
    Ouvre et ferme sa propre connexion indépendante pour ne jamais interférer
    avec la transaction ou la boucle principale d'ordonnancement.
    """
    now = time.time()
    if now - last_run_timestamp >= interval_seconds:
        report = run_retention(config=config, db_path=db_path)
        return now, report
    return last_run_timestamp, None


# --- Interface Ligne de Commande (CLI) ---

def main():
    parser = argparse.ArgumentParser(
        description="Outil de surveillance et rétention pathologique de la base SQLite (Cluster-CI v3 - Règle A15)."
    )
    parser.add_argument("--db-path", default=None, help="Chemin vers cluster_scheduler.db")
    parser.add_argument("--threshold-gb", type=float, default=1.0, help="Seuil pathologique en Go (défaut: 1.0 Go)")
    parser.add_argument("--days", type=int, default=365, help="Ancienneté max des jobs terminés en jours (défaut: 365)")
    parser.add_argument("--batch-size", type=int, default=100, help="Taille des lots de purge (défaut: 100)")
    parser.add_argument("--dry-run", action="store_true", help="Prévisualiser sans supprimer")
    parser.add_argument("--force", action="store_true", help="Forcer la purge même si la base < 1 Go")
    parser.add_argument("--json", action="store_true", help="Sortie structurée JSON")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    cfg = RetentionConfig(
        pathological_threshold_bytes=int(args.threshold_gb * 1024 * 1024 * 1024),
        retention_days=args.days,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        force=args.force,
    )

    report = run_retention(config=cfg, db_path=args.db_path)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        prefix = "[DRY-RUN] " if report.dry_run else ""
        print(f"\n{prefix}=== Rapport de Rétention Cluster-CI (Règle A15) ===")
        print(f"Horodatage            : {report.timestamp}")
        print(f"Durée                 : {report.duration_s:.3f} s")
        print(f"Taille DB             : {report.db_size_bytes / (1024*1024):.2f} Mo")
        print(f"Seuil pathologique    : {report.pathological_threshold_bytes / (1024*1024*1024):.1f} Go")
        print(f"Croissance pathologique : {report.pathological_growth_detected}")
        print(f"Jobs analysés         : {report.jobs_analyzed}")
        print(f"Jobs actifs protégés  : {report.active_jobs_preserved}")
        print(f"Jobs purgés           : {report.jobs_purged}")
        print(f"Lignes DB supprimées  : {report.rows_deleted}")
        if report.errors:
            print(f"Erreurs               : {report.errors}")


if __name__ == "__main__":
    main()
