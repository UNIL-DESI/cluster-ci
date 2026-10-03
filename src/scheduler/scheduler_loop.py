import time
import json
import requests
import os
import socket
import subprocess
import sys
import shutil
import datetime as dt
from datetime import datetime
try:
    from persistence import (
        get_db_conn, init_db, update_dag_ready_states, mark_node_status,
        handle_missing_deps, record_runner_heartbeat, check_runner_heartbeat_timeouts,
        get_job_node, get_all_job_nodes, get_aggregated_job_status
    )
    from defaults import (
        DEFAULT_DOCKER_IMAGE, DEFAULT_CPUS, DEFAULT_RAM_GB, DEFAULT_VRAM_GB,
        DEFAULT_STORAGE_GB, ALLOW_PACKING, OS_HEADROOM_GB,
        RUNNER_HEARTBEAT_TIMEOUT_S, MAX_WORKERS_PER_JOB, ALLOWED_RESOURCE_KEYS
    )
    from artifact_registry import affinity_bytes, sources_for, record_node_outputs
except ImportError:
    from src.scheduler.persistence import (
        get_db_conn, init_db, update_dag_ready_states, mark_node_status,
        handle_missing_deps, record_runner_heartbeat, check_runner_heartbeat_timeouts,
        get_job_node, get_all_job_nodes, get_aggregated_job_status
    )
    from src.scheduler.defaults import (
        DEFAULT_DOCKER_IMAGE, DEFAULT_CPUS, DEFAULT_RAM_GB, DEFAULT_VRAM_GB,
        DEFAULT_STORAGE_GB, ALLOW_PACKING, OS_HEADROOM_GB,
        RUNNER_HEARTBEAT_TIMEOUT_S, MAX_WORKERS_PER_JOB, ALLOWED_RESOURCE_KEYS
    )
    from src.scheduler.artifact_registry import affinity_bytes, sources_for, record_node_outputs
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def cancel_job_cleanly(job_id, exit_code=-15):
    """
    Cancels a job cleanly from the scheduler loop:
    - Contacts all active workers to kill containers
    - Cancels GH Action workflow (best effort)
    - Updates DB status to failed and marks unfinished DAG nodes as blocked
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT j.*, w.service_url
            FROM jobs j
            LEFT JOIN workers w ON j.worker_id = w.worker_id
            WHERE j.job_id = ?
        ''', (job_id,))
        job_row = cursor.fetchone()

    if not job_row:
        return False

    job = dict(job_row)
    status = job['status']
    if status not in ['pending', 'assigned', 'running']:
        return False

    # 1. Worker cancellation if active on worker(s)
    worker_urls = set()
    if job.get('parallel_mode'):
        try:
            active_ids = json.loads(job.get('active_workers') or '[]')
        except Exception:
            active_ids = []
        if job.get('home_worker') and job['home_worker'] not in active_ids:
            active_ids.append(job['home_worker'])
        if active_ids:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                placeholders = ','.join(['?'] * len(active_ids))
                cursor.execute(f'SELECT service_url FROM workers WHERE worker_id IN ({placeholders})', active_ids)
                for row in cursor.fetchall():
                    if row['service_url']:
                        worker_urls.add(row['service_url'])
    elif status in ['assigned', 'running'] and job.get('service_url'):
        worker_urls.add(job['service_url'])

    for s_url in worker_urls:
        try:
            requests.post(f"{s_url}/cancel/{job_id}", timeout=10)
        except Exception as e:
            logger.error(f"Failed to send cancel to worker {s_url} for job {job_id}: {e}")

    # Si job en parallel_mode, basculer tous les nœuds non terminés à 'blocked'
    if job.get('parallel_mode'):
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE job_nodes
                SET status = 'blocked'
                WHERE job_id = ? AND status NOT IN ('done', 'skipped')
            ''', (job_id,))
            cursor.execute('DELETE FROM runner_heartbeats WHERE job_id = ?', (job_id,))
            cursor.execute('UPDATE workers SET assigned_job_id = NULL WHERE assigned_job_id = ?', (job_id,))
            conn.commit()

    # 2. GHA cancellation (best effort)
    if job.get('gh_run_id'):
        try:
            repo = job['repo']
            run_id = job['gh_run_id']
            gh_token = job.get('gh_token') or os.environ.get("GITHUB_PAT")
            if gh_token:
                headers = {
                    "Authorization": f"token {gh_token}",
                    "Accept": "application/vnd.github.v3+json"
                }
                gh_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel"
                requests.post(gh_url, headers=headers, timeout=5)
        except Exception as e:
            logger.error(f"Failed to cancel GH Action for job {job_id}: {e}")

    # 3. Update DB
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE jobs
            SET status = 'failed', exit_code = ?, finished_at = CURRENT_TIMESTAMP
            WHERE job_id = ?
        ''', (exit_code, job_id))
        conn.commit()

    return True

def orchestrate_cluster_update(job):
    """
    Executes the cluster update orchestration when a maintenance barrier activates:
    1. Runs update routines for workers/headnode (e.g., scripts/cluster_update.py or update_cluster.sh).
    2. Monitors online workers to ensure they recover, restart their agents, and report active heartbeats.
    3. Returns True on success, False on fatal failure.
    """
    job_id = job['job_id']
    target_repo = job.get('repo') or 'UNIL-DESI'
    branch = job.get('branch') or 'main'
    logger.info(f"🛠️ [MAINTENANCE ORCHESTRATION] Starting node update orchestration for job {job_id} ({target_repo}@{branch})...")
    
    update_success = True
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    update_script_py = os.path.join(base_dir, "scripts", "cluster_update.py")
    update_script_sh = os.path.join(base_dir, "update_cluster.sh")
    
    try:
        if os.path.exists(update_script_py):
            logger.info(f"🛠️ [MAINTENANCE] Executing update via {update_script_py} (force execution mode)...")
            cmd = [sys.executable, update_script_py, "--force", "--target-repo", target_repo, "--branch", branch]
            proc = subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, timeout=600)
            logger.info(f"🛠️ [MAINTENANCE] Update process exited with code {proc.returncode}")
            if proc.returncode != 0:
                logger.warning(f"🛠️ [MAINTENANCE] Update stderr: {proc.stderr[:500]}")
        elif os.path.exists(update_script_sh):
            logger.info(f"🛠️ [MAINTENANCE] Executing update via {update_script_sh}...")
            if shutil.which("bash"):
                proc = subprocess.run(["bash", update_script_sh, "--force"], cwd=base_dir, capture_output=True, text=True, timeout=600)
                logger.info(f"🛠️ [MAINTENANCE] Update script exited with code {proc.returncode}")
    except Exception as ex:
        logger.error(f"🛠️ [MAINTENANCE] Error executing cluster update: {ex}")
        update_success = False

    # 2. Verification phase: verify online workers return and send heartbeats
    logger.info("🛠️ [MAINTENANCE] Verifying worker node recovery and fresh heartbeats...")
    verification_timeout = 90  # seconds
    start_verify = time.time()
    all_workers_healthy = False
    
    while time.time() - start_verify < verification_timeout:
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT COUNT(*) FROM workers
                    WHERE status = 'online' AND last_seen >= datetime('now', '-30 seconds')
                ''')
                active_heartbeats = cursor.fetchone()[0]
                if active_heartbeats > 0:
                    logger.info(f"✅ [MAINTENANCE] {active_heartbeats} online worker(s) reporting active heartbeats.")
                    all_workers_healthy = True
                    break
        except Exception as e:
            logger.error(f"Error checking worker heartbeats during maintenance: {e}")
        time.sleep(5)
        
    return update_success or all_workers_healthy

def is_unified_memory(worker):
    """Détecte si un worker dispose d'une architecture à mémoire unifiée (ex: NVIDIA Grace-Blackwell GB10 / DGX Spark)."""
    if worker.get('unified_memory') == 1:
        return True
    gpu_name = (worker.get('gpu_name') or '').lower()
    hostname = (worker.get('hostname') or '').lower()
    return any(k in (gpu_name + hostname) for k in ('grace', 'gb10', 'spark', 'unified'))

def parse_vram_per_gpu(worker):
    """Extrait la liste de VRAM (en Go) par GPU physique du worker."""
    raw = worker.get('vram_per_gpu')
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [float(x) for x in parsed]
        except Exception:
            pass
    gpu_count = worker.get('gpu_count') or 0
    total_vram = worker.get('total_vram_gb') or 0.0
    if gpu_count > 0 and total_vram > 0:
        return [float(total_vram)] * gpu_count
    return []

def is_worker_admissible_for_node(worker, node_resources):
    """
    Règle d'admission universelle :
    - Mémoire unifiée : ram_gb + vram_gb <= total_ram_gb - 8.0 Go
    - Mémoire discrète : ram_gb <= (total_ram_gb - 2.0) et vram_gb <= somme(vram GPUs)
    - CPUs : cpus <= worker.cpus
    - Stockage : storage_gb <= worker.storage
    - Architecture : arm64 vs x86_64
    - Workers autorisés : whitelist
    """
    if not isinstance(node_resources, dict):
        node_resources = {}

    # 1. Whitelist de workers
    allowed = node_resources.get("workers")
    if allowed:
        w_id = worker.get("worker_id", "")
        w_host = worker.get("hostname", "")
        if w_id not in allowed and w_host not in allowed:
            return False

    # 2. Architecture
    w_arch = worker.get("arch") or ("aarch64" if "arm" in (worker.get("hostname", "") + (worker.get("gpu_name") or "")).lower() else "x86_64")
    if node_resources.get("image_arm64") and w_arch not in ("aarch64", "arm64"):
        return False
    if node_resources.get("image_amd64") and not node_resources.get("image_arm64") and w_arch in ("aarch64", "arm64"):
        return False

    # 3. CPUs
    req_cpus = node_resources.get("cpus") or DEFAULT_CPUS
    w_cpus = worker.get("cpus") or 4
    if req_cpus > w_cpus:
        return False

    # 4. Stockage
    req_storage = node_resources.get("storage_gb") or 0.0
    if req_storage > 0:
        w_storage = worker.get("disk_free_gb") or worker.get("available_storage_gb") or 999999.0
        if req_storage > w_storage:
            return False

    # 5. Mémoire
    req_ram = float(node_resources.get("ram_gb") if node_resources.get("ram_gb") is not None else DEFAULT_RAM_GB)
    req_vram = float(node_resources.get("vram_gb") if node_resources.get("vram_gb") is not None else DEFAULT_VRAM_GB)
    total_ram = float(worker.get("total_ram_gb") or 0.0)

    if is_unified_memory(worker):
        # Pool partagé unique : ram + vram <= total - 8.0
        if (req_ram + req_vram) > (total_ram - OS_HEADROOM_GB):
            return False
    else:
        # Machine discrète
        if req_ram > (total_ram - 2.0):
            return False
        if req_vram > 0:
            gpus = parse_vram_per_gpu(worker)
            if not gpus or sum(gpus) < req_vram:
                return False

    return True

def allocate_gpus(worker, node_resources):
    """
    Attribue la liste des indices de GPU physiques (CUDA_VISIBLE_DEVICES) :
    - Mémoire unifiée : []
    - Machine discrète sans VRAM : []
    - Machine discrète avec VRAM : sélectionne le plus petit ensemble de GPU suffisant.
    """
    if is_unified_memory(worker):
        return []
    if not isinstance(node_resources, dict):
        return []
    req_vram = float(node_resources.get("vram_gb") or 0.0)
    if req_vram <= 0:
        return []

    gpus = parse_vram_per_gpu(worker)
    if not gpus:
        return None

    # Chercher si un seul GPU suffit
    single_candidates = [(i, v) for i, v in enumerate(gpus) if v >= req_vram]
    if single_candidates:
        single_candidates.sort(key=lambda x: x[1])
        return [single_candidates[0][0]]

    # Sinon combinaison minimale
    allocated = []
    cum_vram = 0.0
    for i, v in sorted(enumerate(gpus), key=lambda x: x[1], reverse=True):
        allocated.append(i)
        cum_vram += v
        if cum_vram >= req_vram:
            return sorted(allocated)

    return None

def get_data_affinity_score(job, worker):
    """
    Amendement A5 : Calcule le score d'affinité des données sur ce worker.
    Utilise affinity_bytes d'artifact_registry complété par les out_paths de job_nodes.
    """
    total_score = 0
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT dep_paths FROM job_nodes
            WHERE job_id = ? AND status = 'ready'
        ''', (job["job_id"],))
        ready_nodes = cursor.fetchall()
        if not ready_nodes:
            return 0

        all_dep_hashes = []
        for r in ready_nodes:
            dp_raw = r["dep_paths"]
            if dp_raw:
                try:
                    parsed = json.loads(dp_raw)
                    if isinstance(parsed, list):
                        all_dep_hashes.extend(parsed)
                except Exception:
                    pass

        if all_dep_hashes:
            try:
                bytes_aff = affinity_bytes(conn, all_dep_hashes, worker.get("worker_id"))
                total_score += bytes_aff
            except Exception as e:
                logger.debug(f"affinity_bytes check failed: {e}")

        cursor.execute('''
            SELECT out_paths FROM job_nodes
            WHERE job_id = ? AND status = 'done' AND worker_id = ?
        ''', (job["job_id"], worker.get("worker_id")))
        done_nodes = cursor.fetchall()

    done_paths = set()
    for row in done_nodes:
        raw = row["out_paths"]
        if raw:
            try:
                for o in json.loads(raw):
                    p = o.get("path") if isinstance(o, dict) else o
                    if p: done_paths.add(p)
            except Exception:
                pass

    for dp in all_dep_hashes:
        if dp in done_paths:
            total_score += 1

    return total_score

def get_oldest_ready_node_time(job):
    """Renvoie l'horodatage ou l'identifiant pour départager les nœuds prêts les plus anciens."""
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT started_at, job_id FROM job_nodes
            WHERE job_id = ? AND status = 'ready'
            ORDER BY priority DESC, node_name ASC
            LIMIT 1
        ''', (job["job_id"],))
        row = cursor.fetchone()
        return job.get("created_at") or ""

def validate_plan(plan_data):
    """
    Valide un plan JSON v3 à la soumission :
    - clés de ressources autorisées uniquement
    - nœuds et dépendances cohérents
    - absence de cycle dans le DAG
    Lève ValueError si invalide.
    """
    if not isinstance(plan_data, dict):
        raise ValueError("Le plan doit être un objet JSON")

    nodes = plan_data.get("nodes")
    if not isinstance(nodes, list) or len(nodes) == 0:
        raise ValueError("Le plan doit contenir une liste non vide 'nodes'")

    defaults = plan_data.get("defaults", {})
    if isinstance(defaults, dict):
        unknown_defaults = set(defaults.keys()) - ALLOWED_RESOURCE_KEYS
        if unknown_defaults:
            raise ValueError(f"Clé de ressource inconnue dans defaults: {unknown_defaults}")

    node_names = set()
    for n in nodes:
        name = n.get("name")
        if not name:
            raise ValueError("Chaque nœud doit avoir un 'name'")
        if name in node_names:
            raise ValueError(f"Nom de nœud dupliqué dans le plan: '{name}'")
        node_names.add(name)

        resources = n.get("resources", {})
        if isinstance(resources, dict):
            unknown_res = set(resources.keys()) - ALLOWED_RESOURCE_KEYS
            if unknown_res:
                raise ValueError(f"Clé de ressource inconnue pour le nœud '{name}': {unknown_res}")

    # Vérification des dépendances déclarées
    adj = {name: [] for name in node_names}
    for n in nodes:
        name = n["name"]
        for dep in n.get("deps", []):
            if dep not in node_names:
                raise ValueError(f"Dépendance invalide '{dep}' déclarée par le nœud '{name}'")
            adj[dep].append(name)

    # Détection de cycle (DFS 3 couleurs : 0=blanc, 1=gris, 2=noir)
    visited = {name: 0 for name in node_names}
    def dfs(u):
        visited[u] = 1
        for v in adj[u]:
            if visited[v] == 1:
                return True
            if visited[v] == 0:
                if dfs(v): return True
        visited[u] = 2
        return False

    for name in node_names:
        if visited[name] == 0:
            if dfs(name):
                raise ValueError(f"Cycle détecté dans le DAG des nœuds à partir de '{name}'")

    return True

def _release_worker_from_job(job_id, worker_id):
    """Retire un worker de la liste des active_workers d'un job et libère assigned_job_id."""
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT active_workers FROM jobs WHERE job_id = ?', (job_id,))
        row = cursor.fetchone()
        if row:
            try:
                active = json.loads(row["active_workers"] or "[]")
                if worker_id in active:
                    active.remove(worker_id)
                    cursor.execute('UPDATE jobs SET active_workers = ? WHERE job_id = ?',
                                   (json.dumps(active), job_id))
            except Exception:
                pass
        cursor.execute('UPDATE workers SET assigned_job_id = NULL WHERE worker_id = ?', (worker_id,))
        conn.commit()

def handle_next_node(req):
    """
    Point névralgique de transition et d'équité (POST /api/jobs/<job_id>/next_node).
    Actions retournées : 'run' | 'switch_image' | 'yield' | 'finish' | 'wait'
    """
    job_id = req.get("job_id")
    worker_id = req.get("worker")
    runner_id = req.get("runner_id")
    node_name = req.get("node")
    status = req.get("status")
    duration_s = req.get("duration_s")
    exit_code = req.get("exit_code")
    error_message = req.get("error_message")
    missing_paths = req.get("missing_paths") or []
    current_image = req.get("current_image")

    # 1. Enregistrement résultat nœud précédent
    if node_name:
        if status == "done":
            mark_node_status(job_id, node_name, "done", duration_s=duration_s, exit_code=0)
            outputs_to_record = req.get("outputs") or req.get("out_paths")
            if outputs_to_record:
                try:
                    with get_db_conn() as conn:
                        record_node_outputs(conn, job_id, node_name, worker_id, outputs_to_record)
                except Exception as e:
                    logger.debug(f"Failed to record node outputs in artifact registry: {e}")
        elif status == "failed":
            mark_node_status(job_id, node_name, "failed", duration_s=duration_s,
                             exit_code=exit_code or 1, error_message=error_message)
        elif status == "missing_deps":
            res = handle_missing_deps(job_id, node_name, missing_paths)
            if not res.get("success"):
                mark_node_status(job_id, node_name, "failed", exit_code=1,
                                 error_message=f"Missing deps could not be recovered: {missing_paths}")

    # Récupérer l'état du job et du worker
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM jobs WHERE job_id = ?', (job_id,))
        job_row = cursor.fetchone()
        if not job_row:
            return {"action": "finish", "error": "Job not found"}
        job = dict(job_row)

        cursor.execute('SELECT * FROM workers WHERE worker_id = ?', (worker_id,))
        worker_row = cursor.fetchone()
        if not worker_row:
            return {"action": "finish", "error": "Worker not found"}
        worker = dict(worker_row)

        # Récupérer les workers inactifs disponibles actuellement (pour vérifier si une machine inactive peut accueillir un concurrent)
        cursor.execute('''
            SELECT * FROM workers
            WHERE status = 'online'
            AND last_seen >= datetime('now', '-60 seconds')
            AND worker_id != ?
            AND worker_id NOT IN (
                SELECT worker_id FROM jobs WHERE status IN ('running', 'assigned') AND worker_id IS NOT NULL
            )
            AND (assigned_job_id IS NULL OR assigned_job_id = '')
        ''', (worker_id,))
        idle_other_workers = [dict(r) for r in cursor.fetchall()]

        cursor.execute('SELECT * FROM job_nodes WHERE job_id = ?', (job_id,))
        job_nodes = [dict(r) for r in cursor.fetchall()]

    active_workers = json.loads(job.get("active_workers") or "[]")
    is_home = (worker_id == job.get("home_worker"))

    # 2. Équité aux frontières de nœuds (uniquement sur machine supplémentaire, jamais sur machine prioritaire)
    if not is_home:
        # A2 : Vérifier les jobs classiques en attente (détiennent 0 machine et veulent 1 machine)
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM jobs
                WHERE status = 'pending' AND (parallel_mode = 0 OR parallel_mode IS NULL)
                ORDER BY created_at ASC
            ''')
            pending_classic_jobs = [dict(r) for r in cursor.fetchall()]

        for c_job in pending_classic_jobs:
            c_res = {"ram_gb": c_job.get("ram_required_gb", 0), "vram_gb": c_job.get("vram_required_gb", 0)}
            if c_job.get("allowed_workers"):
                try: c_res["workers"] = json.loads(c_job["allowed_workers"])
                except Exception: pass
            if is_worker_admissible_for_node(worker, c_res):
                # Vérifier si une autre machine idle peut déjà l'accueillir
                can_idle_host = any(is_worker_admissible_for_node(iw, c_res) for iw in idle_other_workers)
                if not can_idle_host:
                    _release_worker_from_job(job_id, worker_id)
                    return {"action": "yield"}

        # A1 : Anti ping-pong entre jobs parallèles
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM jobs
                WHERE status IN ('pending', 'assigned', 'running') AND parallel_mode = 1 AND job_id != ?
            ''', (job_id,))
            competing_parallel_jobs = [dict(r) for r in cursor.fetchall()]

        current_machine_count = len(active_workers)
        for comp_job in competing_parallel_jobs:
            comp_active = json.loads(comp_job.get("active_workers") or "[]")
            comp_count = len(comp_active)
            if comp_count + 1 < current_machine_count:
                # Vérifier si comp_job a un nœud ready admissible sur ce worker
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT resources FROM job_nodes
                        WHERE job_id = ? AND status = 'ready'
                    ''', (comp_job["job_id"],))
                    ready_comp_nodes = cursor.fetchall()

                admissible_for_comp = False
                for rcn in ready_comp_nodes:
                    rcn_res = json.loads(rcn["resources"]) if rcn["resources"] else {}
                    if is_worker_admissible_for_node(worker, rcn_res):
                        admissible_for_comp = True
                        break

                if admissible_for_comp:
                    can_idle_host = False
                    for iw in idle_other_workers:
                        for rcn in ready_comp_nodes:
                            rcn_res = json.loads(rcn["resources"]) if rcn["resources"] else {}
                            if is_worker_admissible_for_node(iw, rcn_res):
                                can_idle_host = True
                                break
                        if can_idle_host:
                            break

                    if not can_idle_host:
                        _release_worker_from_job(job_id, worker_id)
                        return {"action": "yield"}

    # 3. Sélection du prochain nœud intra-job
    ready_nodes = [n for n in job_nodes if n["status"] == "ready"]
    admissible_ready_nodes = []
    for n in ready_nodes:
        n_res = json.loads(n["resources"]) if n["resources"] else {}
        if is_worker_admissible_for_node(worker, n_res):
            admissible_ready_nodes.append((n, n_res))

    if not admissible_ready_nodes:
        # Aucun nœud prêt admissible
        if is_home:
            has_unresolved = any(n["status"] in ("pending", "ready", "running") for n in job_nodes)
            if not has_unresolved:
                return {"action": "finish"}
            return {"action": "wait"}
        else:
            _release_worker_from_job(job_id, worker_id)
            return {"action": "yield"}

    def node_sort_key(item):
        n, res = item
        raw_deps = n.get("deps")
        deps = json.loads(raw_deps) if raw_deps else []
        is_direct_child = (node_name in deps) if node_name else False
        n_image = n.get("image") or res.get("image")
        same_image = (n_image == current_image) if current_image else False
        priority = float(n.get("priority", 0.0))
        return (
            1 if (is_direct_child and same_image) else 0,
            1 if same_image else 0,
            1 if is_direct_child else 0,
            priority
        )

    admissible_ready_nodes.sort(key=node_sort_key, reverse=True)
    next_node, next_res = admissible_ready_nodes[0]

    next_image = next_node.get("image") or next_res.get("image") or DEFAULT_DOCKER_IMAGE
    action = "run" if (current_image and next_image == current_image) else "switch_image"
    gpu_ids = allocate_gpus(worker, next_res)

    # Marquer le nœud running
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE job_nodes
            SET status = 'running', worker_id = ?, runner_id = ?, started_at = CURRENT_TIMESTAMP
            WHERE job_id = ? AND node_name = ?
        ''', (worker_id, runner_id, job_id, next_node["node_name"]))
        cursor.execute('''
            UPDATE jobs
            SET status = 'running', started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
            WHERE job_id = ? AND status IN ('pending', 'assigned')
        ''', (job_id,))
        conn.commit()

    if runner_id:
        record_runner_heartbeat(job_id, runner_id, worker_id, next_node["node_name"])

    dep_paths_list = []
    raw_deps = next_node.get("dep_paths")
    if raw_deps:
        try:
            dep_paths_list = json.loads(raw_deps) if isinstance(raw_deps, str) else list(raw_deps)
        except Exception:
            dep_paths_list = []

    dep_sources_map = {}
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT worker_id, service_url FROM workers WHERE status = 'online'")
            online_workers_map = {r[0]: r[1] for r in cursor.fetchall()}
            dep_sources_map = sources_for(conn, dep_paths_list, online_workers_map)
    except Exception as e:
        logger.debug(f"Failed to query artifact sources: {e}")

    return {
        "action": action,
        "node": next_node["node_name"],
        "image": next_image,
        "resources": next_res,
        "gpu_ids": gpu_ids,
        "dep_paths": dep_paths_list,
        "dep_sources": dep_sources_map,
        "sources": dep_sources_map
    }

def schedule_iteration():
    """
    Exécute une itération complète de planification :
    1. Nettoyage préliminaire (ghost workers, orphan jobs, watchdog, timeout heartbeats runner, DAG update)
    2. Gestion barrière de maintenance
    3. Ordonnancement des jobs parallèles v3 (home worker + machines supplémentaires + équité)
    4. Ordonnancement des jobs classiques (chemin historique préservé + correction GB10 unifié)
    """
    expired_jobs = []
    pending_jobs = []
    workers = []

    # Reprise des runners sans heartbeat et propagation DAG
    check_runner_heartbeat_timeouts()
    update_dag_ready_states()

    with get_db_conn() as conn:
        cursor = conn.cursor()

        # 0. Ghost Workers cleanup
        cursor.execute('''
            UPDATE workers SET status = 'offline'
            WHERE status = 'online' AND last_seen < datetime('now', '-120 seconds')
        ''')
        conn.commit()

        # 1. Cleanup orphaned running/assigned jobs
        cursor.execute('''
            UPDATE jobs
            SET status = 'failed', exit_code = COALESCE(exit_code, -99)
            WHERE status IN ('running', 'assigned') 
            AND worker_id IN (
                SELECT worker_id FROM workers 
                WHERE status = 'offline' OR last_seen < datetime('now', '-300 seconds')
            )
        ''')
        conn.commit()

        # 1.5. Watchdog de durée
        cursor.execute('''
            SELECT job_id, repo, branch, status, started_at, created_at, max_runtime_hours
            FROM jobs
            WHERE status IN ('running', 'assigned')
        ''')
        active_jobs = [dict(row) for row in cursor.fetchall()]
        for job in active_jobs:
            job_id = job['job_id']
            max_hours = job['max_runtime_hours'] or 24.0
            start_time_str = job['started_at'] or job['created_at']
            if not start_time_str:
                continue
            try:
                start_t = datetime.strptime(start_time_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
                now_utc = dt.datetime.utcnow()
                elapsed_seconds = (now_utc - start_t).total_seconds()
                limit_seconds = (max_hours * 3600) + 300
                if elapsed_seconds > limit_seconds:
                    expired_jobs.append(job_id)
            except Exception as ex:
                logger.error(f"Watchdog parsing error for job {job_id}: {ex}")

        # Maintenance en cours
        cursor.execute('''
            SELECT * FROM jobs
            WHERE status = 'running' AND (job_type = 'maintenance' OR is_maintenance = 1)
            LIMIT 1
        ''')
        running_maintenance = cursor.fetchone()

        # Pending jobs classiques ou racine
        cursor.execute('''
            SELECT * FROM jobs
            WHERE status = "pending"
            ORDER BY (CASE WHEN job_type = 'maintenance' OR is_maintenance = 1 THEN 0 ELSE 1 END) ASC, created_at ASC
        ''')
        pending_jobs = [dict(row) for row in cursor.fetchall()]

        # Workers en ligne et non occupés
        cursor.execute('''
            SELECT * FROM workers
            WHERE status = "online"
            AND last_seen >= datetime('now', '-60 seconds')
            AND worker_id NOT IN (
                SELECT worker_id FROM jobs
                WHERE status IN ('running', 'assigned')
                AND worker_id IS NOT NULL
            )
            AND (assigned_job_id IS NULL OR assigned_job_id = '')
            ORDER BY total_ram_gb DESC
        ''')
        workers = [dict(row) for row in cursor.fetchall()]

    for job_id in expired_jobs:
        try:
            cancel_job_cleanly(job_id, exit_code=-15)
        except Exception as e:
            logger.error(f"Watchdog failed to cancel job {job_id}: {e}")

    if running_maintenance:
        return

    # Maintenance barrier
    if pending_jobs and (pending_jobs[0].get('job_type') == 'maintenance' or pending_jobs[0].get('is_maintenance') == 1):
        head_job = pending_jobs[0]
        maint_job_id = head_job['job_id']
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM jobs
                WHERE status IN ('running', 'assigned')
                AND (job_type != 'maintenance' OR job_type IS NULL)
                AND (is_maintenance = 0 OR is_maintenance IS NULL)
            ''')
            active_compute_jobs = cursor.fetchone()[0]
        if active_compute_jobs > 0:
            return

        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE jobs SET status = "running", started_at = CURRENT_TIMESTAMP WHERE job_id = ?', (maint_job_id,))
            conn.commit()
        success = orchestrate_cluster_update(head_job)
        with get_db_conn() as conn:
            cursor = conn.cursor()
            final_status = 'completed' if success else 'failed'
            exit_code = 0 if success else 1
            cursor.execute('UPDATE jobs SET status = ?, exit_code = ?, finished_at = CURRENT_TIMESTAMP WHERE job_id = ?', (final_status, exit_code, maint_job_id))
            conn.commit()
        return

    if not workers:
        return

    # 3. Ordonnancement :
    # 3.1 D'abord les jobs parallèles sans home_worker (ordre FIFO)
    job_active_map = {}
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM jobs
            WHERE parallel_mode = 1 AND status IN ('pending', 'assigned', 'running')
            ORDER BY created_at ASC
        ''')
        parallel_jobs = [dict(r) for r in cursor.fetchall()]

    for p_job in parallel_jobs:
        jid = p_job["job_id"]
        hw = p_job.get("home_worker")
        try:
            act = json.loads(p_job.get("active_workers") or "[]")
        except Exception:
            act = []
        if hw and hw not in act:
            act.append(hw)
        job_active_map[jid] = act

        if not hw:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT resources FROM job_nodes WHERE job_id = ? AND status = "ready"', (jid,))
                ready_rows = cursor.fetchall()

            for w in list(workers):
                can_run = any(is_worker_admissible_for_node(w, json.loads(rr["resources"]) if rr["resources"] else {}) for rr in ready_rows)
                if can_run:
                    hw = w["worker_id"]
                    if hw not in job_active_map[jid]:
                        job_active_map[jid].append(hw)
                    with get_db_conn() as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            UPDATE jobs
                            SET home_worker = ?, active_workers = ?, worker_id = COALESCE(worker_id, ?),
                                status = CASE WHEN status = 'pending' THEN 'assigned' ELSE status END
                            WHERE job_id = ?
                        ''', (hw, json.dumps(job_active_map[jid]), hw, jid))
                        cursor.execute('UPDATE workers SET assigned_job_id = ? WHERE worker_id = ?', (jid, hw))
                        conn.commit()
                    workers = [rem for rem in workers if rem["worker_id"] != hw]
                    p_job["home_worker"] = hw
                    break

    # 3.2 Garantie 1ère machine pour les jobs classiques en attente (Amendement A2)
    # Un job classique en attente détient 0 machine : il reçoit sa machine AVANT
    # que des machines supplémentaires ne soient distribuées aux jobs qui en possèdent déjà.
    classic_pending = [j for j in pending_jobs if not j.get('parallel_mode')]
    for c_job in list(classic_pending):
        if not workers:
            break
        c_id = c_job['job_id']
        ram_required = c_job['ram_required_gb']
        vram_required = c_job.get('vram_required_gb') or 0
        repo = c_job['repo']
        job_branch = c_job.get('branch', '')
        required_hashes = json.loads(c_job.get('required_hashes') or '[]')
        allowed_workers_raw = c_job.get('allowed_workers')
        allowed_workers = json.loads(allowed_workers_raw) if allowed_workers_raw else None

        # Exclusivité de branche entre jobs distincts
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM jobs
                WHERE repo = ? AND branch = ? AND status IN ('running', 'assigned')
                AND job_id != ?
            ''', (repo, job_branch, c_id))
            if cursor.fetchone()[0] > 0:
                continue

        # Filtrage RAM/VRAM avec correction GB10 unifié
        candidates = []
        for w in workers:
            if is_unified_memory(w):
                if (w['total_ram_gb'] - OS_HEADROOM_GB) >= (ram_required + vram_required):
                    if not allowed_workers or w.get('hostname', '') in allowed_workers:
                        candidates.append(w)
            else:
                if (w['total_ram_gb'] - OS_HEADROOM_GB) >= ram_required:
                    if vram_required == 0 or (w.get('total_vram_gb') or 0) >= vram_required:
                        if not allowed_workers or w.get('hostname', '') in allowed_workers:
                            candidates.append(w)

        if not candidates:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT MAX(total_ram_gb) FROM workers WHERE status = "online"')
                max_total = cursor.fetchone()[0] or 0.0
                cursor.execute('SELECT MAX(total_vram_gb) FROM workers WHERE status = "online"')
                max_vram = cursor.fetchone()[0] or 0.0

            if ram_required > (max_total - OS_HEADROOM_GB) or (vram_required > 0 and vram_required > max_vram):
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE jobs SET status = 'failed' WHERE job_id = ?", (c_id,))
                    conn.commit()
            continue

        # Data Locality (P2P Discovery)
        worker_scores = []
        headnode_hostname = socket.gethostname()
        for worker in candidates:
            score = 0
            if required_hashes and worker.get('service_url'):
                try:
                    resp = requests.post(f"{worker['service_url']}/check_cache",
                                         json={"repo": repo, "hashes": required_hashes},
                                         timeout=2)
                    if resp.status_code == 200:
                        score = len(resp.json())
                except Exception as e:
                    logger.warning(f"Failed to check cache on worker {worker['worker_id']}: {e}")

            svc_url = worker.get('service_url') or ''
            worker_hostname = worker.get('hostname', '')
            if (worker_hostname == headnode_hostname or 'localhost' in svc_url or '127.0.0.1' in svc_url):
                score -= 1
            worker_scores.append((worker, score))

        worker_scores.sort(key=lambda x: x[1], reverse=True)
        assigned_worker, winner_score = worker_scores[0]

        p2p_url = None
        if winner_score < len(required_hashes) and len(worker_scores) > 1:
            peers = [ws for ws in worker_scores if ws[0]['worker_id'] != assigned_worker['worker_id']]
            if peers and peers[0][1] > 0 and peers[0][0].get('service_url'):
                p2p_url = f"{peers[0][0]['service_url']}/fetch_artifact".replace("1300.223.169.200", "130.223.169.200")

        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE jobs
                SET status = 'assigned', worker_id = ?, p2p_url = ?
                WHERE job_id = ? AND status = 'pending'
            ''', (assigned_worker['worker_id'], p2p_url, c_id))
            if cursor.rowcount > 0:
                conn.commit()
                workers = [w for w in workers if w['worker_id'] != assigned_worker['worker_id']]
                classic_pending.remove(c_job)

    # 3.3 Répartition équitable des machines supplémentaires aux jobs parallèles
    for w in list(workers):
        eligible_jobs = []
        for p_job in parallel_jobs:
            jid = p_job["job_id"]
            current_active = job_active_map.get(jid, [])
            if len(current_active) >= MAX_WORKERS_PER_JOB:
                continue

            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT resources FROM job_nodes WHERE job_id = ? AND status = "ready"', (jid,))
                ready_rows = cursor.fetchall()

            can_run_any = any(is_worker_admissible_for_node(w, json.loads(rr["resources"]) if rr["resources"] else {}) for rr in ready_rows)
            if can_run_any:
                eligible_jobs.append((p_job, current_active))

        if eligible_jobs:
            best_job, cur_act = min(
                eligible_jobs,
                key=lambda item: (
                    len(item[1]),
                    get_oldest_ready_node_time(item[0]),
                    -get_data_affinity_score(item[0], w)
                )
            )
            jid = best_job["job_id"]
            if w["worker_id"] not in cur_act:
                cur_act.append(w["worker_id"])
            job_active_map[jid] = cur_act
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE jobs SET active_workers = ? WHERE job_id = ?
                ''', (json.dumps(cur_act), jid))
                cursor.execute('UPDATE workers SET assigned_job_id = ? WHERE worker_id = ?', (jid, w["worker_id"]))
                conn.commit()
            workers = [rem for rem in workers if rem["worker_id"] != w["worker_id"]]

def schedule_jobs():
    """Boucle continue d'ordonnancement cadencée toutes les 5 secondes."""
    while True:
        try:
            schedule_iteration()
        except Exception as e:
            logger.error(f"Error in scheduler loop: {e}")
        time.sleep(5)

if __name__ == '__main__':
    init_db()
    schedule_jobs()

