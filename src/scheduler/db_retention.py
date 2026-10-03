"""
Module de rétention et purge automatisée de la base SQLite et des journaux du headnode (Cluster-CI v3).

Garantit que la base de données (cluster_scheduler.db) et l'espace disque du headnode
(journaux de jobs, logs agrégés v3, runner heartbeats, tables v3 job_nodes et node_artifacts)
ne croissent jamais indéfiniment.

Règles de sécurité strictes :
- AUCUN job actif (pending, assigned, running, queued) n'est JAMAIS purgé.
- Purge en cascade ordonnée des tables dépendantes : runner_heartbeats, node_artifacts, job_nodes, jobs.
- Purge des journaux de jobs sur disque associés (job_logs/{job_id}.log) et archives locales uploadées.
- Plafonnement et rotation des logs individuels géants (logs agrégés v3).
- Transactions courtes par lots pour ne jamais verrouiller la base SQLite WAL.
- Récupération d'espace disque (incremental_vacuum ou VACUUM + wal_checkpoint).
- Mode --dry-run pour prévisualisation sécurisée.
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
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger("cluster_ci.db_retention")

# Statuts protégés : un job dans l'un de ces statuts n'est JAMAIS purgé
ACTIVE_JOB_STATUSES = frozenset({"pending", "running", "assigned", "queued"})

# Statuts terminaux éligibles à la purge selon les critères d'âge et de plafond
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "stopped"})


@dataclass
class RetentionConfig:
    """Configuration de la politique de rétention."""
    retention_days: int = 30
    max_retained_jobs: int = 2000
    min_retained_jobs: int = 500
    batch_size: int = 100
    max_job_log_bytes: int = 10 * 1024 * 1024  # 10 Mo par fichier log
    vacuum_mode: str = "auto"  # "auto", "incremental", "full", "checkpoint", "none"
    incremental_vacuum_pages: int = 1000
    purge_local_archives: bool = True
    rotate_runs_log: bool = True
    log_dir: str | Path | None = None
    dry_run: bool = False

    @classmethod
    def from_env(cls, **overrides) -> RetentionConfig:
        """Instancie la configuration depuis les variables d'environnement."""
        retention_days = int(os.environ.get("CLUSTER_RETENTION_DAYS", "30"))
        max_retained_jobs = int(os.environ.get("CLUSTER_MAX_RETAINED_JOBS", "2000"))
        min_retained_jobs = int(os.environ.get("CLUSTER_MIN_RETAINED_JOBS", "500"))
        batch_size = int(os.environ.get("CLUSTER_RETENTION_BATCH_SIZE", "100"))
        vacuum_mode = os.environ.get("CLUSTER_RETENTION_VACUUM", "auto")
        dry_run = os.environ.get("CLUSTER_RETENTION_DRY_RUN", "0") in ("1", "true", "True")
        log_dir = os.environ.get("CLUSTER_LOGS_DIR", None)

        cfg_dict: dict[str, Any] = {
            "retention_days": retention_days,
            "max_retained_jobs": max_retained_jobs,
            "min_retained_jobs": min_retained_jobs,
            "batch_size": batch_size,
            "vacuum_mode": vacuum_mode,
            "dry_run": dry_run,
            "log_dir": log_dir,
        }
        cfg_dict.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**cfg_dict)


@dataclass
class RetentionReport:
    """Rapport d'exécution de la purge."""
    dry_run: bool = False
    timestamp: str = ""
    duration_s: float = 0.0
    jobs_analyzed: int = 0
    active_jobs_preserved: int = 0
    terminal_jobs_total: int = 0
    jobs_purged: int = 0
    rows_deleted: dict[str, int] = field(default_factory=dict)
    files_deleted: int = 0
    bytes_freed_disk: int = 0
    db_size_before_bytes: int = 0
    db_size_after_bytes: int = 0
    db_bytes_freed: int = 0
    vacuum_performed: str = "none"
    logs_truncated_count: int = 0
    logs_truncated_bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "timestamp": self.timestamp,
            "duration_s": round(self.duration_s, 3),
            "jobs_analyzed": self.jobs_analyzed,
            "active_jobs_preserved": self.active_jobs_preserved,
            "terminal_jobs_total": self.terminal_jobs_total,
            "jobs_purged": self.jobs_purged,
            "rows_deleted": self.rows_deleted,
            "files_deleted": self.files_deleted,
            "bytes_freed_disk": self.bytes_freed_disk,
            "db_size_before_bytes": self.db_size_before_bytes,
            "db_size_after_bytes": self.db_size_after_bytes,
            "db_bytes_freed": self.db_bytes_freed,
            "vacuum_performed": self.vacuum_performed,
            "logs_truncated_count": self.logs_truncated_count,
            "logs_truncated_bytes_freed": self.logs_truncated_bytes_freed,
            "errors": self.errors,
        }


def get_existing_tables(conn: sqlite3.Connection) -> set[str]:
    """Retourne la liste des tables utilisateur présentes dans la base SQLite."""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    return {row[0] for row in cursor.fetchall()}


def resolve_default_logs_dir() -> Path:
    """Localise le dossier standard des logs de jobs sur le headnode."""
    env_dir = os.environ.get("CLUSTER_LOGS_DIR")
    if env_dir:
        return Path(env_dir)
    # BASE_DIR standard : racine du dépôt cluster-ci
    current_file = Path(__file__).resolve()
    # current_file = .../src/scheduler/db_retention.py -> repo_root = 3 parents up
    repo_root = current_file.parent.parent.parent
    candidate = repo_root / "job_logs"
    return candidate


def parse_sqlite_timestamp(ts_val: Any) -> datetime | None:
    """Convertit un horodatage SQLite textuel en objet datetime UTC."""
    if not ts_val:
        return None
    if isinstance(ts_val, datetime):
        if ts_val.tzinfo is None:
            return ts_val.replace(tzinfo=timezone.utc)
        return ts_val.astimezone(timezone.utc)
    ts_str = str(ts_val).strip()
    # Formats courants SQLite: "YYYY-MM-DD HH:MM:SS", "YYYY-MM-DDTHH:MM:SS", "YYYY-MM-DD HH:MM:SS.ffffff"
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
    Identifie les jobs éligibles à la purge en respectant les priorités et invariants :
    1. Aucun job actif n'est jamais candidat.
    2. Respecte le seuil d'âge (retention_days).
    3. Respecte le plafond max (max_retained_jobs).
    4. Protège le plancher minimum (min_retained_jobs).

    Retourne : (candidates_to_purge, total_analyzed, active_preserved_count)
    """
    cursor = conn.cursor()
    
    # 1. Compter les jobs actifs
    cursor.execute("""
        SELECT count(*) FROM jobs 
        WHERE status IN ('pending', 'running', 'assigned', 'queued')
    """)
    active_count = cursor.fetchone()[0]

    # 2. Récupérer tous les jobs terminés ordonnés du plus ancien au plus récent
    # Pour déterminer l'âge d'un job, on utilise finished_at en priorité, sinon created_at
    cursor.execute("""
        SELECT job_id, status, created_at, finished_at, local_archive_path 
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
            "local_archive_path": r[4] if len(r) > 4 else None,
        }
        for r in rows
    ]

    total_terminal = len(terminal_jobs)
    total_analyzed = active_count + total_terminal

    if total_terminal <= config.min_retained_jobs:
        # Plancher atteint : on conserve l'ensemble des jobs terminés
        logger.info(
            f"Terminal jobs ({total_terminal}) <= min_retained_jobs ({config.min_retained_jobs}). "
            "No jobs will be purged."
        )
        return [], total_analyzed, active_count

    cutoff_date = now - timedelta(days=config.retention_days)
    candidates_by_age: list[dict[str, Any]] = []

    for job in terminal_jobs:
        dt_ref = parse_sqlite_timestamp(job["finished_at"]) or parse_sqlite_timestamp(job["created_at"])
        if dt_ref and dt_ref < cutoff_date:
            candidates_by_age.append(job)

    # Vérification du plafond max_retained_jobs
    # Si le nombre de jobs terminés restants après purge par âge dépasse encore max_retained_jobs,
    # on sélectionne les plus anciens pour ramener au plafond.
    candidates_set = {j["job_id"]: j for j in candidates_by_age}

    remaining_count = total_terminal - len(candidates_set)
    if remaining_count > config.max_retained_jobs:
        overflow = remaining_count - config.max_retained_jobs
        for job in terminal_jobs:
            if overflow <= 0:
                break
            if job["job_id"] not in candidates_set:
                candidates_set[job["job_id"]] = job
                overflow -= 1

    # Appliquer le plancher de sécurité strict min_retained_jobs
    final_candidates = list(candidates_set.values())
    max_allowable_purges = max(0, total_terminal - config.min_retained_jobs)
    if len(final_candidates) > max_allowable_purges:
        # Garder les plus anciens d'abord, mais ne pas dépasser max_allowable_purges
        final_candidates = final_candidates[:max_allowable_purges]

    # Invariant de sécurité absolu : vérifier qu'aucun candidat n'a un statut actif
    verified_candidates = []
    for cand in final_candidates:
        if cand["status"] in ACTIVE_JOB_STATUSES:
            logger.error(f"CRITICAL SAFETY VIOLATION: Candidate job {cand['job_id']} has active status {cand['status']}. Skipping!")
            continue
        verified_candidates.append(cand)

    return verified_candidates, total_analyzed, active_count


def purge_jobs_from_database(
    conn: sqlite3.Connection,
    job_ids: Sequence[str],
    dry_run: bool = False,
    batch_size: int = 100,
) -> dict[str, int]:
    """
    Supprime les jobs et toutes les lignes associées dans les tables dépendantes
    par transactions courtes pour ne pas bloquer les autres processus SQLite WAL.

    Tables ciblées dans l'ordre strict de dépendance :
    1. runner_heartbeats (job_id)
    2. node_artifacts (job_id)
    3. job_nodes (job_id)
    4. workers (assigned_job_id mis à NULL)
    5. jobs (job_id)
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
            # Mode prévisualisation : décompte sans suppression
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
                # Vérifier si assigned_job_id existe dans workers
                cursor.execute("PRAGMA table_info(workers)")
                col_names = {c[1] for c in cursor.fetchall()}
                if "assigned_job_id" in col_names:
                    cursor.execute(f"SELECT count(*) FROM workers WHERE assigned_job_id IN ({placeholders})", chunk)
                    rows_deleted["workers_disassociated"] += cursor.fetchone()[0]

            cursor.execute(f"SELECT count(*) FROM jobs WHERE job_id IN ({placeholders})", chunk)
            rows_deleted["jobs"] += cursor.fetchone()[0]

        else:
            # Mode réel : transaction courte par lot
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


def purge_associated_disk_files(
    candidate_jobs: Sequence[dict[str, Any]],
    log_dir: Path,
    purge_local_archives: bool = True,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Supprime les fichiers journaux ({job_id}.log) et les archives de téléversement locales
    associés aux jobs purgés.

    Retourne : (files_deleted_count, bytes_freed)
    """
    files_deleted = 0
    bytes_freed = 0

    for job in candidate_jobs:
        job_id = job["job_id"]

        # 1. Fichier de log du job
        log_file = log_dir / f"{job_id}.log"
        if log_file.is_file():
            try:
                sz = log_file.stat().st_size
                if not dry_run:
                    log_file.unlink(missing_ok=True)
                files_deleted += 1
                bytes_freed += sz
            except Exception as e:
                logger.warning(f"Could not remove log file {log_file}: {e}")

        # 2. Archive locale (local_archive_path)
        if purge_local_archives:
            archive_path_str = job.get("local_archive_path")
            paths_to_check = []
            if archive_path_str:
                paths_to_check.append(Path(archive_path_str))
            
            # Vérifier aussi l'emplacement standard repositories/_local_uploads/{job_id}.tar.gz
            repo_root = log_dir.parent
            std_upload = repo_root / "repositories" / "_local_uploads" / f"{job_id}.tar.gz"
            paths_to_check.append(std_upload)

            for p in paths_to_check:
                if p.is_file():
                    try:
                        sz = p.stat().st_size
                        if not dry_run:
                            p.unlink(missing_ok=True)
                        files_deleted += 1
                        bytes_freed += sz
                    except Exception as e:
                        logger.warning(f"Could not remove archive file {p}: {e}")

    return files_deleted, bytes_freed


def truncate_large_job_logs(
    log_dir: Path,
    max_log_bytes: int,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Inspecte les fichiers de logs individuels présents dans log_dir.
    Si un fichier dépasse max_log_bytes (ex: 10 Mo généré par un job verbeux),
    il est tronqué pour conserver le début (1 Mo) et la fin (2 Mo),
    libérant ainsi l'espace excédentaire.

    Retourne : (logs_truncated_count, bytes_freed)
    """
    if not log_dir.is_dir() or max_log_bytes <= 0:
        return 0, 0

    truncated_count = 0
    freed_bytes = 0
    head_bytes = 1024 * 1024       # 1 Mo au début
    tail_bytes = 2 * 1024 * 1024   # 2 Mo à la fin

    try:
        for log_file in log_dir.glob("*.log"):
            if not log_file.is_file():
                continue
            try:
                size = log_file.stat().st_size
                if size > max_log_bytes:
                    diff = size - (head_bytes + tail_bytes)
                    if diff > 0:
                        truncated_count += 1
                        freed_bytes += diff
                        if not dry_run:
                            with open(log_file, "rb") as f:
                                head_data = f.read(head_bytes)
                                f.seek(-tail_bytes, os.SEEK_END)
                                tail_data = f.read(tail_bytes)
                            marker = f"\n\n--- [TRUNCATED {diff} BYTES BY CLUSTER-CI RETENTION POLICY] ---\n\n".encode("utf-8")
                            with open(log_file, "wb") as f:
                                f.write(head_data)
                                f.write(marker)
                                f.write(tail_data)
            except Exception as e:
                logger.warning(f"Could not check/truncate log file {log_file}: {e}")
    except Exception as e:
        logger.warning(f"Error scanning logs directory {log_dir}: {e}")

    return truncated_count, freed_bytes


def rotate_file_if_large(
    file_path: Path,
    max_bytes: int = 10 * 1024 * 1024,
    dry_run: bool = False,
) -> int:
    """Effectue une rotation simple d'un fichier volumineux (ex: cluster-ci-runs.log)."""
    if not file_path.is_file():
        return 0
    try:
        size = file_path.stat().st_size
        if size > max_bytes:
            if not dry_run:
                rotated_path = file_path.with_suffix(file_path.suffix + ".1")
                if rotated_path.exists():
                    rotated_path.unlink()
                file_path.rename(rotated_path)
            return size
    except Exception as e:
        logger.warning(f"Failed to rotate {file_path}: {e}")
    return 0


def reclaim_sqlite_space(
    conn: sqlite3.Connection,
    db_path: Path | None,
    vacuum_mode: str = "auto",
    incremental_pages: int = 1000,
    dry_run: bool = False,
) -> str:
    """
    Exécute les opérations de récupération d'espace disque SQLite :
    - Mode 'incremental' : PRAGMA incremental_vacuum
    - Mode 'full' ou 'auto' : VACUUM ou checkpoint WAL selon configuration
    - PRAGMA wal_checkpoint(TRUNCATE) pour compacter le fichier -wal.
    """
    if dry_run or vacuum_mode == "none":
        return "none"

    cursor = conn.cursor()
    action_taken = "none"

    # Vérification auto_vacuum
    cursor.execute("PRAGMA auto_vacuum;")
    auto_vac_row = cursor.fetchone()
    auto_vacuum_val = auto_vac_row[0] if auto_vac_row else 0

    cursor.execute("PRAGMA freelist_count;")
    freelist_count = cursor.fetchone()[0]

    if auto_vacuum_val == 2:  # INCREMENTAL
        cursor.execute(f"PRAGMA incremental_vacuum({incremental_pages});")
        action_taken = "incremental"
    elif vacuum_mode in ("full", "auto") and freelist_count > 50:
        # VACUUM nécessite d'être hors transaction
        try:
            conn.commit()
            conn.execute("VACUUM;")
            action_taken = "full"
        except sqlite3.OperationalError as e:
            logger.warning(f"VACUUM skipped (might be locked or busy): {e}")

    # Checkpoint WAL pour forcer le déversement et la troncature du fichier -wal
    try:
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        if action_taken == "none":
            action_taken = "checkpoint"
    except Exception as e:
        logger.warning(f"wal_checkpoint failed: {e}")

    return action_taken


def run_retention(
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
    config: RetentionConfig | None = None,
    db_path: str | Path | None = None,
) -> RetentionReport:
    """
    Point d'entrée principal pour la rétention et purge du headnode.
    
    Peut être invoqué :
    - Directement avec une connexion SQLite ouverte `conn`
    - Périodiquement depuis la boucle du scheduler (`scheduler_loop.py`)
    - En ligne de commande CLI (avec db_path et options)
    """
    t_start = time.time()
    now_utc = now or datetime.now(timezone.utc)
    cfg = config or RetentionConfig.from_env()

    report = RetentionReport(
        dry_run=cfg.dry_run,
        timestamp=now_utc.isoformat(),
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
        # Tentative de récupération du chemin de fichier si disponible
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA database_list;")
            for row in cursor.fetchall():
                if row[1] == "main" and row[2]:
                    resolved_db_path = Path(row[2])
                    break
        except Exception:
            pass

    log_dir = Path(cfg.log_dir).resolve() if cfg.log_dir else resolve_default_logs_dir()

    # Mesurer la taille initiale de la DB
    if resolved_db_path and resolved_db_path.is_file():
        report.db_size_before_bytes = resolved_db_path.stat().st_size

    try:
        # 1. Sélection des candidats éligibles
        candidates, total_analyzed, active_preserved = select_candidate_jobs(conn, now_utc, cfg)
        report.jobs_analyzed = total_analyzed
        report.active_jobs_preserved = active_preserved
        report.terminal_jobs_total = total_analyzed - active_preserved
        report.jobs_purged = len(candidates)

        candidate_ids = [c["job_id"] for c in candidates]

        # 2. Purge en cascade dans la base SQLite
        rows_deleted = purge_jobs_from_database(
            conn=conn,
            job_ids=candidate_ids,
            dry_run=cfg.dry_run,
            batch_size=cfg.batch_size,
        )
        report.rows_deleted = rows_deleted

        # 3. Purge des fichiers journaux et archives associées
        files_del, bytes_disk = purge_associated_disk_files(
            candidate_jobs=candidates,
            log_dir=log_dir,
            purge_local_archives=cfg.purge_local_archives,
            dry_run=cfg.dry_run,
        )
        report.files_deleted = files_del
        report.bytes_freed_disk = bytes_disk

        # 4. Troncature et plafonnement des logs individuels volumineux
        trunc_count, trunc_bytes = truncate_large_job_logs(
            log_dir=log_dir,
            max_log_bytes=cfg.max_job_log_bytes,
            dry_run=cfg.dry_run,
        )
        report.logs_truncated_count = trunc_count
        report.logs_truncated_bytes_freed = trunc_bytes
        report.bytes_freed_disk += trunc_bytes

        # 5. Rotation de cluster-ci-runs.log
        if cfg.rotate_runs_log and log_dir.parent:
            runs_log_path = log_dir.parent / "cluster-ci-runs.log"
            rotated_sz = rotate_file_if_large(runs_log_path, dry_run=cfg.dry_run)
            if rotated_sz > 0:
                report.files_deleted += 1

        # 6. Récupération d'espace disque SQLite
        if not cfg.dry_run and len(candidate_ids) > 0:
            vac_mode = reclaim_sqlite_space(
                conn=conn,
                db_path=resolved_db_path,
                vacuum_mode=cfg.vacuum_mode,
                incremental_pages=cfg.incremental_vacuum_pages,
                dry_run=cfg.dry_run,
            )
            report.vacuum_performed = vac_mode

        # Mesurer la taille finale de la DB
        if resolved_db_path and resolved_db_path.is_file():
            report.db_size_after_bytes = resolved_db_path.stat().st_size
            report.db_bytes_freed = max(0, report.db_size_before_bytes - report.db_size_after_bytes)

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
    conn: sqlite3.Connection | None = None,
) -> tuple[float, RetentionReport | None]:
    """
    Helper pour exécution cadencée dans la boucle du scheduler.
    
    Exemple d'intégration dans `scheduler_loop.py` :
    ```python
    # Initialisation hors boucle :
    last_retention_time = 0.0

    # Dans la boucle périodique schedule_iteration() :
    last_retention_time, report = run_retention_periodic(
        last_run_timestamp=last_retention_time,
        interval_seconds=86400.0,  # 1x par jour
        conn=conn,
    )
    if report and report.jobs_purged > 0:
        logger.info(f"🧹 Retention completed: {report.jobs_purged} jobs purged.")
    ```
    """
    now = time.time()
    if now - last_run_timestamp >= interval_seconds:
        report = run_retention(conn=conn, config=config)
        return now, report
    return last_run_timestamp, None


# --- Interface Ligne de Commande (CLI) ---

def main():
    parser = argparse.ArgumentParser(
        description="Outil de rétention et purge automatisée de la base SQLite et des journaux de Cluster-CI."
    )
    parser.add_argument("--db-path", default=None, help="Chemin vers cluster_scheduler.db")
    parser.add_argument("--days", type=int, default=None, help="Nombre de jours de rétention (défaut: 30)")
    parser.add_argument("--max-jobs", type=int, default=None, help="Plafond max de jobs conservés (défaut: 2000)")
    parser.add_argument("--min-jobs", type=int, default=None, help="Plancher de sécurité de jobs (défaut: 500)")
    parser.add_argument("--batch-size", type=int, default=100, help="Taille des lots de purge (défaut: 100)")
    parser.add_argument("--log-dir", default=None, help="Répertoire des journaux de jobs (défaut: ./job_logs)")
    parser.add_argument("--dry-run", action="store_true", help="Prévisualiser sans supprimer")
    parser.add_argument("--vacuum", choices=["auto", "incremental", "full", "checkpoint", "none"], default="auto", help="Mode de compactage SQLite")
    parser.add_argument("--json", action="store_true", help="Sortie structurée au format JSON")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    cfg = RetentionConfig.from_env(
        retention_days=args.days,
        max_retained_jobs=args.max_jobs,
        min_retained_jobs=args.min_jobs,
        batch_size=args.batch_size,
        log_dir=args.log_dir,
        dry_run=args.dry_run,
        vacuum_mode=args.vacuum,
    )

    report = run_retention(config=cfg, db_path=args.db_path)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        prefix = "[DRY-RUN] " if report.dry_run else ""
        print(f"\n{prefix}=== Rapport de Rétention Cluster-CI ===")
        print(f"Horodatage          : {report.timestamp}")
        print(f"Durée               : {report.duration_s:.3f} s")
        print(f"Jobs analysés       : {report.jobs_analyzed}")
        print(f"Jobs actifs protégés: {report.active_jobs_preserved}")
        print(f"Jobs terminés total : {report.terminal_jobs_total}")
        print(f"Jobs purgés         : {report.jobs_purged}")
        print(f"Lignes DB purgées   : {report.rows_deleted}")
        print(f"Fichiers supprimés  : {report.files_deleted}")
        print(f"Disque libéré       : {report.bytes_freed_disk / (1024*1024):.2f} Mo")
        print(f"Taille DB avant     : {report.db_size_before_bytes / (1024*1024):.2f} Mo")
        print(f"Taille DB après     : {report.db_size_after_bytes / (1024*1024):.2f} Mo")
        print(f"Espace DB libéré    : {report.db_bytes_freed / (1024*1024):.2f} Mo")
        print(f"Compactage SQLite   : {report.vacuum_performed}")
        if report.errors:
            print(f"Erreurs             : {report.errors}")


if __name__ == "__main__":
    main()
