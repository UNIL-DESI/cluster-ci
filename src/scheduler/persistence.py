import sqlite3
import os
from contextlib import contextmanager

def get_db_path():
    return os.environ.get("CLUSTER_DB_PATH", "cluster_scheduler.db")

DB_PATH = get_db_path()

def init_db():
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    conn.execute('pragma journal_mode=wal')
    conn.execute('pragma synchronous=normal')
    cursor = conn.cursor()

    # Workers Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS workers (
            worker_id TEXT PRIMARY KEY,
            hostname TEXT,
            service_url TEXT,
            total_ram_gb REAL,
            available_ram_gb REAL,
            total_storage_gb REAL,
            available_storage_gb REAL,
            total_vram_gb REAL,
            gpu_count INTEGER,
            gpu_name TEXT,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'online'
        )
    ''')

    # Add service_url if it doesn't exist (migration)
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN service_url TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # Add storage columns if they don't exist (migration)
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN total_storage_gb REAL')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN available_storage_gb REAL')
    except sqlite3.OperationalError:
        pass

    # Add GPU/VRAM columns if they don't exist (migration)
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN total_vram_gb REAL')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN gpu_name TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN gpu_count INTEGER')
    except sqlite3.OperationalError:
        pass

    # Jobs Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            repo TEXT,
            branch TEXT,
            commit_hash TEXT,
            ram_required_gb REAL,
            status TEXT DEFAULT 'pending',
            worker_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            exit_code INTEGER,
            viewer_port INTEGER,
            max_runtime_hours REAL,
            exposed_port INTEGER,
            gh_run_id TEXT,
            required_hashes TEXT,
            p2p_url TEXT,
            gh_token TEXT,
            custom_web_app INTEGER DEFAULT 0,
            job_type TEXT DEFAULT 'compute',
            is_maintenance INTEGER DEFAULT 0,
            FOREIGN KEY (worker_id) REFERENCES workers (worker_id)
        )
    ''')

    # commit_hash migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN commit_hash TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # viewer_port migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN viewer_port INTEGER')
    except sqlite3.OperationalError:
        pass # Already exists

    # required_hashes migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN required_hashes TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # p2p_url migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN p2p_url TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # gh_token migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN gh_token TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # env_vars migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN env_vars TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # username migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN username TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # max_runtime_hours migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN max_runtime_hours REAL')
    except sqlite3.OperationalError:
        pass

    # exposed_port migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN exposed_port INTEGER')
    except sqlite3.OperationalError:
        pass

    # gh_run_id migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN gh_run_id TEXT')
    except sqlite3.OperationalError:
        pass

    # custom_web_app migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN custom_web_app INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # vram_required_gb migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN vram_required_gb REAL')
    except sqlite3.OperationalError:
        pass

    # available_vram_gb migration (dynamic free VRAM reported by worker heartbeats)
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN available_vram_gb REAL DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # allowed_workers migration (JSON list of hostnames to restrict job execution)
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN allowed_workers TEXT')
    except sqlite3.OperationalError:
        pass

    # is_local migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN is_local INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # local_archive_path migration (headnode storage path for uploaded source archive)
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN local_archive_path TEXT')
    except sqlite3.OperationalError:
        pass

    # job_type migration (e.g. 'compute', 'maintenance')
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN job_type TEXT DEFAULT 'compute'")
    except sqlite3.OperationalError:
        pass

    # is_maintenance migration (boolean flag: 1 for maintenance jobs)
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN is_maintenance INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # --- v3 Migrations: Job Nodes table ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS job_nodes (
            job_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            worker_id TEXT,
            runner_id TEXT,
            image TEXT,
            priority REAL DEFAULT 0.0,
            stale INTEGER DEFAULT 1,
            stale_reason TEXT,
            resources TEXT,
            deps TEXT,
            dep_paths TEXT,
            out_paths TEXT,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            duration_s REAL,
            exit_code INTEGER,
            error_message TEXT,
            missing_deps_retried INTEGER DEFAULT 0,
            attempt INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0,
            failure_reason TEXT,
            cas_transfers TEXT DEFAULT '[]',
            PRIMARY KEY (job_id, node_name),
            FOREIGN KEY (job_id) REFERENCES jobs (job_id)
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_job_nodes_status ON job_nodes(job_id, status)')

    # --- v3 Migrations: Jobs table additions ---
    for col_def in [
        'home_worker TEXT',
        'parallel_mode INTEGER DEFAULT 0',
        'plan_json TEXT',
        'active_workers TEXT DEFAULT "[]"',
        'error_message TEXT',
        'gpu_ids TEXT DEFAULT "[]"'
    ]:
        col_name = col_def.split()[0]
        try:
            cursor.execute(f'ALTER TABLE jobs ADD COLUMN {col_def}')
        except sqlite3.OperationalError:
            pass

    # --- v3 Migrations: Job Nodes additions ---
    for col_def in [
        "gpu_ids TEXT DEFAULT '[]'",
        "attempt INTEGER DEFAULT 0",
        "retry_count INTEGER DEFAULT 0",
        "failure_reason TEXT",
        "cas_transfers TEXT DEFAULT '[]'",
    ]:
        try:
            cursor.execute(f"ALTER TABLE job_nodes ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    # --- v3 Migrations: Workers table additions ---
    for col_def in [
        'cpus INTEGER DEFAULT 4',
        'ram_gb REAL DEFAULT 0',
        'vram_per_gpu TEXT',
        'unified_memory INTEGER DEFAULT 0',
        'arch TEXT DEFAULT "x86_64"',
        'disk_free_gb REAL DEFAULT 0',
        'assigned_job_id TEXT DEFAULT NULL',
        'role TEXT DEFAULT "worker"',
        'placement_priority INTEGER DEFAULT 0',
        "docker_images TEXT DEFAULT '{}'"
    ]:
        col_name = col_def.split()[0]
        try:
            cursor.execute(f'ALTER TABLE workers ADD COLUMN {col_def}')
        except sqlite3.OperationalError:
            pass

    # --- v3 Migrations: Runner Heartbeats table ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS runner_heartbeats (
            job_id TEXT NOT NULL,
            runner_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            current_node TEXT,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (job_id, runner_id)
        )
    ''')

    conn.commit()
    conn.close()

@contextmanager
def get_db_conn():
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    conn.execute('pragma journal_mode=wal')
    conn.execute('pragma synchronous=normal')
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

import json
from datetime import datetime
try:
    from src.scheduler.defaults import (
        DEFAULT_DOCKER_IMAGE, DEFAULT_CPUS, DEFAULT_GPUS, DEFAULT_RAM_GB,
        DEFAULT_VRAM_GB, DEFAULT_STORAGE_GB, RUNNER_HEARTBEAT_TIMEOUT_S
    )
except ImportError:
    from defaults import (
        DEFAULT_DOCKER_IMAGE, DEFAULT_CPUS, DEFAULT_GPUS, DEFAULT_RAM_GB,
        DEFAULT_VRAM_GB, DEFAULT_STORAGE_GB, RUNNER_HEARTBEAT_TIMEOUT_S
    )

def init_job_nodes_from_plan(job_id, plan_data):
    """
    Initialise les lignes de job_nodes à partir du plan JSON validé.
    Calcule le statut initial de chaque nœud :
    - skipped si stale == False
    - ready si stale == True et aucune dépendance
    - pending si stale == True et dépendances requises
    """
    nodes = plan_data.get("nodes", [])
    defaults = plan_data.get("defaults", {})

    with get_db_conn() as conn:
        cursor = conn.cursor()
        for node in nodes:
            name = node["name"]
            deps = node.get("deps", [])
            dep_paths = node.get("dep_paths", [])
            out_paths = node.get("out_paths", [])
            priority = float(node.get("priority", 0.0))
            stale = 1 if node.get("stale", True) else 0
            stale_reason = node.get("stale_reason")

            res = dict(node.get("resources", {}))
            image = node.get("image") or res.get("image") or defaults.get("image") or DEFAULT_DOCKER_IMAGE
            merged_res = {
                "image": image,
                "image_arm64": res.get("image_arm64") or defaults.get("image_arm64"),
                "image_amd64": res.get("image_amd64") or defaults.get("image_amd64"),
                "cpus": res.get("cpus") or defaults.get("cpus") or DEFAULT_CPUS,
                "gpus": res.get("gpus") if res.get("gpus") is not None else defaults.get("gpus", DEFAULT_GPUS),
                "ram_gb": res.get("ram_gb") if res.get("ram_gb") is not None else defaults.get("ram_gb", DEFAULT_RAM_GB),
                "vram_gb": res.get("vram_gb") if res.get("vram_gb") is not None else defaults.get("vram_gb", DEFAULT_VRAM_GB),
                "storage_gb": res.get("storage_gb") if res.get("storage_gb") is not None else defaults.get("storage_gb", DEFAULT_STORAGE_GB),
                "workers": res.get("workers") or defaults.get("workers")
            }

            if not stale:
                status = "skipped"
            elif not deps:
                status = "ready"
            else:
                status = "pending"

            cursor.execute('''
                INSERT OR REPLACE INTO job_nodes (
                    job_id, node_name, status, image, priority, stale, stale_reason,
                    resources, deps, dep_paths, out_paths
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                job_id, name, status, image, priority, stale, stale_reason,
                json.dumps(merged_res), json.dumps(deps), json.dumps(dep_paths), json.dumps(out_paths)
            ))
        conn.commit()

    update_dag_ready_states(job_id)

def update_dag_ready_states(job_id=None):
    """
    Met à jour l'état du DAG pour un job donné ou tous les jobs actifs :
    - pending -> ready si tous les parents sont 'done' ou 'skipped'
    - pending / ready -> blocked si au moins un parent est 'failed' ou 'blocked'
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        if job_id:
            cursor.execute('SELECT job_id, node_name, status, deps FROM job_nodes WHERE job_id = ?', (job_id,))
        else:
            cursor.execute('''
                SELECT n.job_id, n.node_name, n.status, n.deps
                FROM job_nodes n
                JOIN jobs j ON n.job_id = j.job_id
                WHERE j.status IN ('pending', 'assigned', 'running')
            ''')
        all_nodes = [dict(row) for row in cursor.fetchall()]

        # Regrouper par job
        jobs_map = {}
        for row in all_nodes:
            jobs_map.setdefault(row["job_id"], {})[row["node_name"]] = row

        updated = False
        for jid, nodes_by_name in jobs_map.items():
            for name, node_row in nodes_by_name.items():
                current_status = node_row["status"]
                if current_status not in ("pending", "ready"):
                    continue

                raw_deps = node_row.get("deps")
                deps = json.loads(raw_deps) if raw_deps else []
                if not deps:
                    if current_status == "pending":
                        cursor.execute('UPDATE job_nodes SET status = "ready" WHERE job_id = ? AND node_name = ?', (jid, name))
                        updated = True
                    continue

                parent_statuses = [nodes_by_name.get(d, {}).get("status") for d in deps]

                # Si au moins un parent a échoué ou est bloqué -> bloquer le descendant
                if any(ps in ("failed", "blocked") for ps in parent_statuses):
                    if current_status != "blocked":
                        cursor.execute('UPDATE job_nodes SET status = "blocked" WHERE job_id = ? AND node_name = ?', (jid, name))
                        node_row["status"] = "blocked"
                        updated = True
                    continue

                # Si tous les parents sont terminés avec succès
                if all(ps in ("done", "skipped") for ps in parent_statuses):
                    if current_status == "pending":
                        cursor.execute('UPDATE job_nodes SET status = "ready" WHERE job_id = ? AND node_name = ?', (jid, name))
                        node_row["status"] = "ready"
                        updated = True

        if updated:
            conn.commit()

def mark_node_status(job_id, node_name, status, duration_s=None, exit_code=None, error_message=None, failure_reason=None, cas_transfers=None):
    """
    Enregistre le statut d'un nœud et propage l'avancement dans le DAG.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cas_json = json.dumps(cas_transfers) if cas_transfers is not None else None
        if status in ("done", "failed"):
            cursor.execute('''
                UPDATE job_nodes
                SET status = ?, duration_s = ?, exit_code = ?, error_message = ?,
                    failure_reason = COALESCE(?, failure_reason),
                    cas_transfers = COALESCE(?, cas_transfers),
                    finished_at = CURRENT_TIMESTAMP, gpu_ids = '[]'
                WHERE job_id = ? AND node_name = ?
            ''', (status, duration_s, exit_code, error_message, failure_reason, cas_json, job_id, node_name))
        else:
            cursor.execute('''
                UPDATE job_nodes
                SET status = ?, duration_s = ?, exit_code = ?, error_message = ?,
                    failure_reason = COALESCE(?, failure_reason),
                    cas_transfers = COALESCE(?, cas_transfers)
                WHERE job_id = ? AND node_name = ?
            ''', (status, duration_s, exit_code, error_message, failure_reason, cas_json, job_id, node_name))
        conn.commit()

    update_dag_ready_states(job_id)

def handle_missing_deps(job_id, consumer_node_name, missing_paths):
    """
    Amendement A4 & Bug 7 : Si l'exécuteur ne peut récupérer une dep_path.
    1. Tente d'abord de localiser l'artefact sur un worker en ligne dans node_artifacts.
       Si trouvé sur des pairs en ligne : NE PAS replanifier le producteur (qui reste 'done'),
       mais autoriser un retry du fetch P2P par le consommateur (missing_deps_retried < 2).
    2. Si AUCUN worker en ligne ne détient l'artefact (ou retries épuisés) :
       Replanifier le producteur avec raison explicite enregistrée (stale_reason = 'outputs_missing_no_peer_cache').
    3. Si le producteur a déjà été relancé : échec explicite du consommateur.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT node_name, status, out_paths, missing_deps_retried FROM job_nodes WHERE job_id = ?', (job_id,))
        nodes = [dict(row) for row in cursor.fetchall()]

    retriggered_node = None
    consumer_node = None
    for n in nodes:
        if n["node_name"] == consumer_node_name:
            consumer_node = n
        raw_outs = n.get("out_paths")
        if not raw_outs:
            continue
        try:
            outs = json.loads(raw_outs)
        except Exception:
            outs = []
        out_file_paths = []
        for o in outs:
            if isinstance(o, dict):
                out_file_paths.append(o.get("path"))
            elif isinstance(o, str):
                out_file_paths.append(o)

        if any(mp in out_file_paths for mp in missing_paths):
            retriggered_node = n

    if not retriggered_node:
        # Aucun producteur identifié, échec direct
        mark_node_status(job_id, consumer_node_name, "failed", exit_code=1,
                         error_message=f"Missing dependencies {missing_paths} with unknown producer")
        return {"success": False, "reason": "unknown_producer"}

    prod_name = retriggered_node["node_name"]
    prod_retried_count = retriggered_node.get("missing_deps_retried", 0)
    consumer_retried_count = (consumer_node or {}).get("missing_deps_retried", 0)

    # Vérifier si des workers en ligne détiennent les artefacts manquants dans node_artifacts
    peer_sources = {}
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT worker_id, service_url FROM workers WHERE status = 'online'")
            online_workers_map = {}
            for r in cursor.fetchall():
                w_id, s_url = r[0], r[1]
                target_url = s_url or w_id
                if target_url:
                    try:
                        from src.runner.fetch_cas_dependencies import normalize_worker_url
                        online_workers_map[w_id] = normalize_worker_url(target_url)
                    except Exception:
                        online_workers_map[w_id] = target_url

            from src.scheduler.artifact_registry import hashes_for_paths, sources_for
            consumer = conn.execute('SELECT is_local FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
            local_consumer = bool(consumer and consumer[0])
            missing_hashes = hashes_for_paths(conn, missing_paths, is_local=local_consumer)

            if missing_hashes and online_workers_map:
                peer_sources = sources_for(conn, missing_hashes, online_workers_map,
                                           is_local=local_consumer)
    except Exception:
        peer_sources = {}

    has_online_peers = any(len(urls) > 0 for urls in peer_sources.values())

    # CAS A : L'artefact réside chez au moins un worker en ligne
    if has_online_peers:
        if consumer_retried_count < 2:
            # Ne JAMAIS replanifier le producteur : donner une nouvelle chance de fetch au consommateur
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE job_nodes
                    SET status = 'ready', worker_id = NULL, runner_id = NULL, gpu_ids = '[]',
                        missing_deps_retried = missing_deps_retried + 1
                    WHERE job_id = ? AND node_name = ?
                ''', (job_id, consumer_node_name))
                conn.commit()

            update_dag_ready_states(job_id)
            return {
                "success": True,
                "action": "retry_fetch",
                "sources": peer_sources,
                "message": f"Artifacts present on online peers. Consumer '{consumer_node_name}' reset to ready for P2P fetch retry."
            }

    # CAS B : Aucun worker en ligne ne détient l'artefact, ou retries P2P du consommateur épuisés
    if prod_retried_count >= 1:
        # Producteur déjà relancé une fois -> échec explicite
        mark_node_status(job_id, consumer_node_name, "failed", exit_code=1,
                         error_message=f"Missing dependencies {missing_paths} could not be recovered after retry of producer {prod_name}")
        return {"success": False, "reason": "retry_exhausted", "producer": prod_name}

    # Relancer le producteur avec raison explicite
    stale_reason = "outputs_missing_no_peer_cache" if not has_online_peers else "outputs_unfetchable_from_peers"
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE job_nodes
            SET status = 'ready', stale_reason = ?,
                worker_id = NULL, runner_id = NULL, gpu_ids = '[]',
                missing_deps_retried = missing_deps_retried + 1
            WHERE job_id = ? AND node_name = ?
        ''', (stale_reason, job_id, prod_name))
        cursor.execute('''
            UPDATE job_nodes
            SET status = 'pending', worker_id = NULL, runner_id = NULL, gpu_ids = '[]'
            WHERE job_id = ? AND node_name = ?
        ''', (job_id, consumer_node_name))
        conn.commit()

    update_dag_ready_states(job_id)
    return {"success": True, "retriggered_node": prod_name, "reason": stale_reason}

def record_runner_heartbeat(job_id, runner_id, worker_id, current_node):
    """Enregistre le heartbeat d'un runner pour un job."""
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO runner_heartbeats (job_id, runner_id, worker_id, current_node, last_seen)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(job_id, runner_id) DO UPDATE SET
                worker_id = excluded.worker_id,
                current_node = excluded.current_node,
                last_seen = CURRENT_TIMESTAMP
        ''', (job_id, runner_id, worker_id, current_node))
        conn.commit()

def check_runner_heartbeat_timeouts(timeout_s=RUNNER_HEARTBEAT_TIMEOUT_S):
    """
    Vérifie les runners actifs. Si aucun heartbeat > timeout_s :
    Le nœud running repasse à ready, worker_id=NULL, runner_id=NULL.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT n.job_id, n.node_name, n.runner_id, n.worker_id,
                   h.last_seen,
                   (strftime('%s', 'now') - strftime('%s', COALESCE(h.last_seen, n.started_at))) as elapsed_s
            FROM job_nodes n
            LEFT JOIN runner_heartbeats h ON n.job_id = h.job_id AND n.runner_id = h.runner_id
            WHERE n.status = 'running'
        ''')
        running_nodes = [dict(row) for row in cursor.fetchall()]

        recovered = []
        for r in running_nodes:
            elapsed = r.get("elapsed_s")
            if elapsed is None or elapsed > timeout_s:
                cursor.execute('''
                    UPDATE job_nodes
                    SET status = 'ready', worker_id = NULL, runner_id = NULL, gpu_ids = '[]'
                    WHERE job_id = ? AND node_name = ?
                ''', (r["job_id"], r["node_name"]))
                cursor.execute('DELETE FROM runner_heartbeats WHERE job_id = ? AND runner_id = ?', (r["job_id"], r["runner_id"]))
                recovered.append(r)

        if recovered:
            conn.commit()
    return recovered

def get_job_node(job_id, node_name):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM job_nodes WHERE job_id = ? AND node_name = ?', (job_id, node_name))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_all_job_nodes(job_id):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM job_nodes WHERE job_id = ? ORDER BY priority DESC, node_name ASC', (job_id,))
        return [dict(row) for row in cursor.fetchall()]

def get_aggregated_job_status(job_id):
    """
    Calcule le statut consolidé du job (completed, failed, running, pending).
    Dérivé strictement de l'agrégat de tous ses nœuds DAG (Bug 10).
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT parallel_mode, status FROM jobs WHERE job_id = ?', (job_id,))
        job = cursor.fetchone()
        if not job:
            return None

        cursor.execute('SELECT node_name, status, stale FROM job_nodes WHERE job_id = ?', (job_id,))
        nodes = [dict(row) for row in cursor.fetchall()]

    if not nodes:
        return job["status"]

    statuses = [n["status"] for n in nodes]

    # 1. Succès complet : 100% des nœuds sont soit done soit skipped
    if all(s in ("done", "skipped") for s in statuses):
        return "completed"

    # 2. Exécution active : au moins un nœud est en cours ou prêt à être assigné
    if any(s in ("running", "ready") for s in statuses):
        return "running"

    # 3. Échec : au moins un échec / blocage et plus aucun nœud ne peut s'exécuter
    if any(s in ("failed", "blocked") for s in statuses):
        return "failed"

    # 4. En cours si au moins un nœud est déjà terminé
    if any(s == "done" for s in statuses):
        return "running"

    return "pending"

