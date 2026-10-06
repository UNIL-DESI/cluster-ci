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
        RUNNER_HEARTBEAT_TIMEOUT_S, MAX_WORKERS_PER_JOB, ALLOWED_RESOURCE_KEYS,
        HEADNODE_RAM_RESERVE_GB, HEADNODE_CPU_RESERVE
    )
    from artifact_registry import affinity_bytes, sources_for, record_node_outputs
    from db_retention import run_retention_periodic
except ImportError:
    from src.scheduler.persistence import (
        get_db_conn, init_db, update_dag_ready_states, mark_node_status,
        handle_missing_deps, record_runner_heartbeat, check_runner_heartbeat_timeouts,
        get_job_node, get_all_job_nodes, get_aggregated_job_status
    )
    from src.scheduler.defaults import (
        DEFAULT_DOCKER_IMAGE, DEFAULT_CPUS, DEFAULT_RAM_GB, DEFAULT_VRAM_GB,
        DEFAULT_STORAGE_GB, ALLOW_PACKING, OS_HEADROOM_GB,
        RUNNER_HEARTBEAT_TIMEOUT_S, MAX_WORKERS_PER_JOB, ALLOWED_RESOURCE_KEYS,
        HEADNODE_RAM_RESERVE_GB, HEADNODE_CPU_RESERVE
    )
    from src.scheduler.artifact_registry import affinity_bytes, sources_for, record_node_outputs
    from src.scheduler.db_retention import run_retention_periodic

try:
    from src.runner.fetch_cas_dependencies import normalize_worker_url
except ImportError:
    try:
        from fetch_cas_dependencies import normalize_worker_url
    except ImportError:
        def normalize_worker_url(worker_str: str, default_port: int = 6000) -> str:
            raw = str(worker_str).strip()
            if not (raw.startswith("http://") or raw.startswith("https://")):
                raw = f"http://{raw}"
            if ":" not in raw.split("//", 1)[-1]:
                raw = f"{raw}:{default_port}"
            return raw
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
                SET status = 'blocked', gpu_ids = '[]'
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
            SET status = 'failed', exit_code = ?, finished_at = CURRENT_TIMESTAMP, gpu_ids = '[]'
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

try:
    from runner.host_guard import (
        placement_priority, is_headnode_host, is_unified_memory_host,
        DEFAULT_PLACEMENT_PRIORITY, HEADNODE_PLACEMENT_PRIORITY
    )
except ImportError:
    from src.runner.host_guard import (
        placement_priority, is_headnode_host, is_unified_memory_host,
        DEFAULT_PLACEMENT_PRIORITY, HEADNODE_PLACEMENT_PRIORITY
    )

def is_unified_memory(worker):
    """Détecte si un worker dispose d'une architecture à mémoire unifiée via host_guard."""
    if not worker or not isinstance(worker, dict):
        return False
    return is_unified_memory_host(worker)

def parse_vram_per_gpu(worker):
    """Extrait la liste de VRAM (en Go) par GPU physique du worker."""
    if not worker or not isinstance(worker, dict):
        return []
    raw = worker.get('vram_per_gpu')
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, list):
                return [float(x) for x in parsed]
            elif isinstance(parsed, dict):
                sorted_keys = sorted(parsed.keys(), key=lambda k: int(k) if str(k).isdigit() else str(k))
                return [float(parsed[k]) for k in sorted_keys]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    gpu_count = worker.get('gpu_count') or 0
    total_vram = worker.get('total_vram_gb') or 0.0
    if gpu_count > 0 and total_vram > 0:
        return [float(total_vram) / float(gpu_count)] * gpu_count
    return []

def get_worker_total_gpus(worker):
    """Extrait le nombre total de GPU physiques équipant le worker."""
    if not worker or not isinstance(worker, dict):
        return 0
    if is_unified_memory(worker):
        cnt = worker.get("gpu_count")
        if cnt is not None and int(cnt) > 0:
            return int(cnt)
        return 1
    vram_list = parse_vram_per_gpu(worker)
    if vram_list:
        return len(vram_list)
    cnt = worker.get("gpu_count")
    if cnt is not None and int(cnt) > 0:
        return int(cnt)
    return 0

def get_worker_available_gpu_ids(worker):
    """Renvoie la liste des identifiants d'indices GPU physiques [0, ..., N-1]."""
    return list(range(get_worker_total_gpus(worker)))

def is_headnode_worker(worker):
    """
    Détecte si un worker représente la machine Headnode via host_guard (A14 : aucune IP/hostname en dur).
    """
    if not worker or not isinstance(worker, dict):
        return False
    return is_headnode_host(worker)

def get_worker_placement_priority(worker):
    """
    Détermine le rang de priorité de placement d'un worker :
    - Convention W11 / Cluster-CI v3 : plus grand = préféré.
    - Calculé exclusivement par src.runner.host_guard.placement_priority(worker).
    - Erreur au démarrage si host_guard est manquant (pas de valeur inventée).
    """
    if not worker or not isinstance(worker, dict):
        return 0
    return int(placement_priority(worker))

def _get_worker_allocated_resources_impl(cursor, worker_id, exclude_node=None):
    if exclude_node and exclude_node[0] and exclude_node[1]:
        cursor.execute('''
            SELECT job_id, node_name, resources, gpu_ids FROM job_nodes
            WHERE worker_id = ? AND status = 'running' AND NOT (job_id = ? AND node_name = ?)
        ''', (worker_id, exclude_node[0], exclude_node[1]))
    else:
        cursor.execute('''
            SELECT job_id, node_name, resources, gpu_ids FROM job_nodes
            WHERE worker_id = ? AND status = 'running'
        ''', (worker_id,))
    running_nodes = cursor.fetchall()

    used_cpus = 0
    used_ram_gb = 0.0
    used_vram_gb = 0.0
    used_storage_gb = 0.0
    allocated_vram_by_gpu = {}
    allocated_gpu_ids = set()
    gpu_holders = {}
    active_executors = len(running_nodes)

    active_job_ids = set()
    for row in running_nodes:
        res_raw = row["resources"]
        res = {}
        if res_raw:
            try:
                res = json.loads(res_raw) if isinstance(res_raw, str) else dict(res_raw)
            except (json.JSONDecodeError, TypeError):
                res = {}
        used_cpus += int(res.get("cpus") or DEFAULT_CPUS)
        used_ram_gb += float(res.get("ram_gb") if res.get("ram_gb") is not None else DEFAULT_RAM_GB)
        node_vram = float(res.get("vram_gb") if res.get("vram_gb") is not None else DEFAULT_VRAM_GB)
        used_vram_gb += node_vram
        used_storage_gb += float(res.get("storage_gb") or 0.0)

        gpu_ids_raw = row["gpu_ids"] if "gpu_ids" in row.keys() else None
        gids = []
        if gpu_ids_raw:
            try:
                gids = json.loads(gpu_ids_raw) if isinstance(gpu_ids_raw, str) else list(gpu_ids_raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                gids = []

        node_name_val = row["node_name"] if "node_name" in row.keys() else "node"
        job_id_val = row["job_id"] if "job_id" in row.keys() else ""
        if job_id_val:
            active_job_ids.add(job_id_val)
        holder_lbl = f"{node_name_val} ({job_id_val[:8]})" if job_id_val else str(node_name_val)

        for gid in gids:
            try:
                gid_int = int(gid)
                allocated_gpu_ids.add(gid_int)
                if gid_int not in gpu_holders:
                    gpu_holders[gid_int] = []
                gpu_holders[gid_int].append(holder_lbl)
            except (TypeError, ValueError):
                pass

        if gids and node_vram > 0:
            per_gpu = node_vram / len(gids)
            for gid in gids:
                try:
                    gid_int = int(gid)
                    allocated_vram_by_gpu[gid_int] = allocated_vram_by_gpu.get(gid_int, 0.0) + per_gpu
                except (TypeError, ValueError):
                    pass

    cursor.execute('''
        SELECT job_id, ram_required_gb, vram_required_gb, gpu_ids FROM jobs
        WHERE worker_id = ? AND status IN ('assigned', 'running') AND (parallel_mode = 0 OR parallel_mode IS NULL)
    ''', (worker_id,))
    classic_jobs = cursor.fetchall()

    for cj in classic_jobs:
        active_executors += 1
        used_cpus += DEFAULT_CPUS
        used_ram_gb += float(cj["ram_required_gb"] or DEFAULT_RAM_GB)
        c_vram = float(cj["vram_required_gb"] or 0.0)
        used_vram_gb += c_vram
        c_job_id = cj["job_id"]
        if c_job_id:
            active_job_ids.add(c_job_id)
        c_lbl = f"Job {c_job_id[:8]}" if c_job_id else "Job classique"

        c_gids = []
        if "gpu_ids" in cj.keys() and cj["gpu_ids"]:
            try:
                c_gids = json.loads(cj["gpu_ids"]) if isinstance(cj["gpu_ids"], str) else list(cj["gpu_ids"])
            except Exception:
                c_gids = []
        if not c_gids and c_vram > 0:
            c_gids = [0]

        for gid in c_gids:
            try:
                gid_int = int(gid)
                allocated_gpu_ids.add(gid_int)
                if gid_int not in gpu_holders:
                    gpu_holders[gid_int] = []
                gpu_holders[gid_int].append(c_lbl)
                if c_vram > 0:
                    allocated_vram_by_gpu[gid_int] = allocated_vram_by_gpu.get(gid_int, 0.0) + (c_vram / len(c_gids))
            except (TypeError, ValueError):
                pass

    return {
        "used_cpus": used_cpus,
        "used_ram_gb": used_ram_gb,
        "used_vram_gb": used_vram_gb,
        "used_storage_gb": used_storage_gb,
        "allocated_vram_by_gpu": allocated_vram_by_gpu,
        "allocated_gpu_ids": allocated_gpu_ids,
        "gpu_holders": gpu_holders,
        "active_executors": active_executors,
        "active_job_ids": active_job_ids
    }

def get_worker_allocated_resources(conn=None, worker_id=None, exclude_node=None):
    """
    Calcule en temps réel les ressources allouées / consommées sur un worker (Packing A11) :
    1. Nœuds de jobs parallèles actuellement en cours ('running')
    2. Jobs classiques assignés ou en cours ('assigned', 'running')
    """
    if conn is not None:
        try:
            cursor = conn.cursor()
            return _get_worker_allocated_resources_impl(cursor, worker_id, exclude_node)
        except Exception:
            pass
    with get_db_conn() as c:
        cursor = c.cursor()
        return _get_worker_allocated_resources_impl(cursor, worker_id, exclude_node)

def allocate_gpus(worker, node_resources, allocated_gpu_ids=None, allocated_vram_by_gpu=None):
    """
    Attribue la liste des indices de GPU physiques (CUDA_VISIBLE_DEVICES) (A16) :
    - Mémoire unifiée : 1 GPU max (device 0). Si req_gpus == 0: retourne [].
      Si req_gpus == 1: retourne [0] si GPU 0 est libre, None si déjà alloué.
      Si req_gpus > 1: retourne None (A16 : mémoire unifiée gpus <= 1).
    - Machine discrète : sélectionne req_gpus GPU libres (et ayant au moins req_vram libre si applicable).
    """
    if not isinstance(node_resources, dict):
        raise TypeError(f"node_resources must be a dict, got {type(node_resources).__name__}")

    # Compatibilité signature si le 3e argument positionnel était un dict de vram
    if isinstance(allocated_gpu_ids, dict):
        allocated_vram_by_gpu = allocated_gpu_ids
        allocated_gpu_ids = set()

    req_gpus = int(node_resources.get("gpus") if node_resources.get("gpus") is not None else 0)
    req_vram = float(node_resources.get("vram_gb") or 0.0)

    # Si vram_gb > 0 mais gpus non spécifié, au moins 1 GPU est sous-entendu (A16)
    if req_vram > 0 and req_gpus == 0:
        req_gpus = 1

    if req_gpus <= 0 and req_vram <= 0:
        return []

    if allocated_gpu_ids is None:
        allocated_gpu_ids = set()
    else:
        allocated_gpu_ids = set(allocated_gpu_ids)

    all_gpu_ids = get_worker_available_gpu_ids(worker)
    if not all_gpu_ids:
        return None

    if is_unified_memory(worker):
        if req_gpus > 1:
            return None  # A16 : mémoire unifiée gpus <= 1
        if 0 in allocated_gpu_ids:
            return None
        return [0]

    if len(all_gpu_ids) < req_gpus:
        return None

    free_gpus = [gid for gid in all_gpu_ids if gid not in allocated_gpu_ids]
    if len(free_gpus) < req_gpus:
        return None

    gpus = parse_vram_per_gpu(worker)
    if req_vram > 0 and gpus:
        if allocated_vram_by_gpu is None:
            allocated_vram_by_gpu = {}

        candidate_gpus = []
        for gid in free_gpus:
            tot_v = gpus[gid] if gid < len(gpus) else 0.0
            used_v = float(allocated_vram_by_gpu.get(gid, 0.0))
            avail = max(0.0, tot_v - used_v)
            if avail >= req_vram:
                candidate_gpus.append((gid, avail))

        if len(candidate_gpus) < req_gpus:
            return None

        # Best-fit : trier par VRAM libre restante croissante qui suffit
        candidate_gpus.sort(key=lambda x: x[1])
        selected = [candidate_gpus[k][0] for k in range(req_gpus)]
        return sorted(selected)

    return sorted(free_gpus[:req_gpus])

def is_worker_admissible_for_node(worker, node_resources, allocated=None):
    """
    Règle d'admission universelle avec PACKING (A11) et vérification stricte NULL (W4) :
    - cpus : used_cpus + req_cpus <= worker.cpus (rejet si worker.cpus est NULL)
    - storage_gb : used_storage + req_storage <= worker.storage (rejet si worker storage est NULL et req > 0)
    - mémoire unifiée : used_mem + (req_ram + req_vram) <= worker.total_ram - 8.0 Go (rejet si worker RAM est NULL)
    - machine discrète : used_ram + req_ram <= worker.total_ram - 2.0 Go, et VRAM restante sur au moins un GPU >= req_vram (rejet si worker VRAM est NULL et req > 0)
    - Architecture & workers whitelist
    """
    if not isinstance(node_resources, dict):
        raise TypeError(f"node_resources must be a dict, got {type(node_resources).__name__}")

    w_id = worker.get("worker_id", "?")

    # 1. Whitelist de workers
    allowed = node_resources.get("workers")
    if allowed:
        w_host = worker.get("hostname", "")
        if w_id not in allowed and w_host not in allowed:
            return False

    # 2. Architecture
    w_arch = worker.get("arch") or ("aarch64" if "arm" in (worker.get("hostname", "") + (worker.get("gpu_name") or "")).lower() else "x86_64")
    if node_resources.get("image_arm64") and w_arch not in ("aarch64", "arm64"):
        return False
    if node_resources.get("image_amd64") and not node_resources.get("image_arm64") and w_arch in ("aarch64", "arm64"):
        return False

    if allocated is None:
        allocated = {
            "used_cpus": 0,
            "used_ram_gb": 0.0,
            "used_vram_gb": 0.0,
            "used_storage_gb": 0.0,
            "allocated_vram_by_gpu": {},
            "active_executors": 0,
            "active_job_ids": set()
        }

    # 2.5 Anti-affinité machine intra-job : un worker exécutant déjà un nœud actif du job J
    # n'est pas éligible pour un autre nœud de J (sérialisation de worker_agent)
    req_job_id = node_resources.get("job_id")
    if req_job_id and allocated:
        active_jobs = allocated.get("active_job_ids") or set()
        if req_job_id in active_jobs:
            logger.debug(f"Worker {w_id} rejected for node: worker already has an active node for job {req_job_id}")
            return False

    # 3. CPUs
    req_cpus = int(node_resources.get("cpus") if node_resources.get("cpus") is not None else DEFAULT_CPUS)
    w_cpus = worker.get("cpus")
    if w_cpus is None:
        logger.info(f"Worker {w_id} rejected for node: cpus={req_cpus} requested but worker CPU count is NULL (unknown)")
        return False
    w_cpus = int(w_cpus)
    if allocated["used_cpus"] + req_cpus > w_cpus:
        logger.debug(f"Worker {w_id} rejected for node: cpus limit exceeded ({allocated['used_cpus']} + {req_cpus} > {w_cpus})")
        return False

    # 4. Stockage (NULL = inconnu -> rejet explicite)
    req_storage = float(node_resources.get("storage_gb") if node_resources.get("storage_gb") is not None else DEFAULT_STORAGE_GB)
    if req_storage > 0:
        w_disk = worker.get("disk_free_gb")
        if w_disk is None:
            w_disk = worker.get("available_storage_gb")
        if w_disk is None:
            logger.info(f"Worker {w_id} rejected for node: storage_gb={req_storage} requested but worker disk capacity is NULL (unknown)")
            return False
        w_disk = float(w_disk)
        if allocated["used_storage_gb"] + req_storage > w_disk:
            logger.debug(f"Worker {w_id} rejected for node: storage limit exceeded ({allocated['used_storage_gb']} + {req_storage} > {w_disk})")
            return False

    # 5. Mémoire (NULL = inconnu -> rejet explicite)
    req_ram = float(node_resources.get("ram_gb") if node_resources.get("ram_gb") is not None else DEFAULT_RAM_GB)
    req_vram = float(node_resources.get("vram_gb") if node_resources.get("vram_gb") is not None else DEFAULT_VRAM_GB)
    req_gpus = int(node_resources.get("gpus") if node_resources.get("gpus") is not None else 0)
    if req_vram > 0 and req_gpus == 0:
        req_gpus = 1
    w_total_ram = worker.get("total_ram_gb")
    if w_total_ram is None:
        logger.info(f"Worker {w_id} rejected for node: ram_gb={req_ram} requested but worker RAM capacity is NULL (unknown)")
        return False
    w_total_ram = float(w_total_ram)

    if is_unified_memory(worker):
        if req_gpus > 1:
            logger.debug(f"Worker {w_id} rejected for node: unified memory does not support gpus > 1 ({req_gpus})")
            return False
        used_mem = allocated["used_ram_gb"] + allocated["used_vram_gb"]
        if used_mem + (req_ram + req_vram) > (w_total_ram - OS_HEADROOM_GB):
            logger.debug(f"Worker {w_id} rejected for node: unified memory exceeded ({used_mem} + {req_ram + req_vram} > {w_total_ram - OS_HEADROOM_GB})")
            return False
        if req_gpus > 0 or req_vram > 0:
            gpu_alloc = allocate_gpus(
                worker, node_resources,
                allocated_gpu_ids=allocated.get("allocated_gpu_ids"),
                allocated_vram_by_gpu=allocated.get("allocated_vram_by_gpu")
            )
            if gpu_alloc is None:
                logger.debug(f"Worker {w_id} rejected for node: unified GPU already held or unavailable")
                return False
    else:
        if allocated["used_ram_gb"] + req_ram > (w_total_ram - 2.0):
            logger.debug(f"Worker {w_id} rejected for node: discrete RAM exceeded ({allocated['used_ram_gb']} + {req_ram} > {w_total_ram - 2.0})")
            return False
        if req_gpus > 0 or req_vram > 0:
            raw_vram = worker.get("vram_per_gpu")
            tot_vram = worker.get("total_vram_gb")
            if raw_vram is None and tot_vram is None:
                logger.info(f"Worker {w_id} rejected for node: vram_gb={req_vram} requested but worker VRAM capacity is NULL (unknown)")
                return False
            gpus = parse_vram_per_gpu(worker)
            if not gpus:
                logger.info(f"Worker {w_id} rejected for node: vram_gb={req_vram}/gpus={req_gpus} requested but worker has no parsed GPUs")
                return False
            gpu_alloc = allocate_gpus(
                worker, node_resources,
                allocated_gpu_ids=allocated.get("allocated_gpu_ids"),
                allocated_vram_by_gpu=allocated.get("allocated_vram_by_gpu")
            )
            if gpu_alloc is None:
                logger.debug(f"Worker {w_id} rejected for node: insufficient available VRAM or GPU count on discrete GPUs")
                return False

    return True

def get_placement_cost(conn=None, worker=None, required_image=None, dep_paths=None):
    """
    Calcule le coût de placement d'un nœud ou job sur un worker (A13) :
    Coût = octets de dépendances absents + taille de l'image Docker si absente.
    Plus le coût est faible, plus le worker est favorisé.
    """
    cost = 0

    # 1. Image Docker locale déclarée dans worker.docker_images (W4)
    raw_images = worker.get("docker_images") if worker else None
    images_dict = {}
    if raw_images:
        try:
            if isinstance(raw_images, str):
                images_dict = json.loads(raw_images)
            elif isinstance(raw_images, dict):
                images_dict = raw_images
        except (json.JSONDecodeError, TypeError, ValueError):
            images_dict = {}

    DEFAULT_IMAGE_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 Go par défaut si image absente
    if required_image:
        if required_image in images_dict:
            cost += 0  # Image déjà présente localement !
        else:
            cost += int(images_dict.get(required_image) or DEFAULT_IMAGE_SIZE_BYTES)

    # 2. Dépendances de données absentes
    if dep_paths and worker:
        try:
            from src.scheduler.artifact_registry import affinity_bytes
            if conn is not None:
                present_bytes = affinity_bytes(conn, dep_paths, worker.get("worker_id"))
            else:
                with get_db_conn() as c:
                    present_bytes = affinity_bytes(c, dep_paths, worker.get("worker_id"))
            total_est_bytes = len(dep_paths) * 100 * 1024 * 1024
            missing_bytes = max(0, total_est_bytes - present_bytes)
            cost += missing_bytes
        except Exception:
            pass

    return cost

def worker_selection_sort_key(conn, worker, allocated, required_image=None, dep_paths=None):
    """
    Clé de tri pour choisir le worker idéal (A13 + W11) :
    1. Rang host_guard le plus élevé d'abord (non-headnode = 50, headnode = 0 en dernier recours).
    2. Coût de placement minimal (image locale présente + dépendances présentes).
    3. Machine libre / non retenue par un job parallèle en priorité.
    4. Moins d'exécuteurs actifs sur la machine.
    5. Plus faible taux d'utilisation de mémoire.
    """
    prio = get_worker_placement_priority(worker)
    cost = get_placement_cost(conn, worker, required_image=required_image, dep_paths=dep_paths)
    w_ram = float(worker.get("total_ram_gb") or 1.0)
    load_ratio = allocated["used_ram_gb"] / w_ram

    wid = worker.get("worker_id")
    is_busy_with_job = 0
    if wid:
        try:
            with get_db_conn() as c:
                cur = c.cursor()
                cur.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('assigned', 'running') AND (home_worker = ? OR active_workers LIKE ?)",
                            (wid, f'%"{wid}"%'))
                is_busy_with_job = cur.fetchone()[0]
        except Exception:
            pass

    return (
        prio,
        -cost,
        -is_busy_with_job,
        -allocated["active_executors"],
        -load_ratio
    )

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
                bytes_aff = affinity_bytes(conn, all_dep_hashes, worker.get("worker_id"),
                                           is_local=bool(job.get('is_local')))
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
        raise ValueError("Plan must be a JSON object")

    # Version v3 acceptée sous forme de chaîne ("3.0") ou nombre
    if "version" in plan_data:
        ver = str(plan_data["version"]).strip()
        if not ver.startswith("3"):
            raise ValueError(f"Unsupported plan version: {plan_data['version']}")

    nodes = plan_data.get("nodes")
    if not isinstance(nodes, list) or len(nodes) == 0:
        raise ValueError("Plan must contain a non-empty 'nodes' list")

    defaults = plan_data.get("defaults", {})
    if isinstance(defaults, dict):
        unknown_defaults = set(defaults.keys()) - ALLOWED_RESOURCE_KEYS
        if unknown_defaults:
            raise ValueError(f"Unknown resource key in defaults: {unknown_defaults}")

    node_names = set()
    for n in nodes:
        name = n.get("name")
        if not name:
            raise ValueError("Each node must have a 'name'")
        if name in node_names:
            raise ValueError(f"Duplicate node name in plan: '{name}'")
        node_names.add(name)

        resources = n.get("resources", {})
        if isinstance(resources, dict):
            unknown_res = set(resources.keys()) - ALLOWED_RESOURCE_KEYS
            if unknown_res:
                raise ValueError(f"Unknown resource key for node '{name}': {unknown_res}")

            # A16 : Cohérence vram_gb et gpus
            if "vram_gb" in resources and float(resources.get("vram_gb") or 0.0) > 0:
                if resources.get("gpus") == 0:
                    raise ValueError(f"Node '{name}' requests vram_gb={resources['vram_gb']} with gpus=0: vram_gb requires gpus >= 1; remedy: declare meta.cluster.gpus >= 1 for stage '{name}' in dvc.yaml")

    # Vérification des dépendances déclarées
    adj = {name: [] for name in node_names}
    for n in nodes:
        name = n["name"]
        for dep in n.get("deps", []):
            if dep not in node_names:
                raise ValueError(f"Invalid dependency '{dep}' declared by node '{name}'")
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
                raise ValueError(f"Cycle detected in DAG nodes starting from '{name}'")

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
    worker_id = req.get("worker") or req.get("worker_id")
    runner_id = req.get("runner_id")
    node_name = req.get("node") or req.get("node_name")
    status = req.get("status")
    duration_s = req.get("duration_s")
    exit_code = req.get("exit_code")
    error_message = req.get("error_message")
    missing_paths = req.get("missing_paths") or req.get("missing_deps") or []
    current_image = req.get("current_image")

    # 1. Enregistrement résultat nœud précédent
    if node_name:
        failure_reason = req.get("failure_reason")
        cas_transfers = req.get("cas_transfers")

        if status == "done":
            mark_node_status(job_id, node_name, "done", duration_s=duration_s, exit_code=0, cas_transfers=cas_transfers)
            outputs_to_record = req.get("outputs") or req.get("out_paths")
            if outputs_to_record:
                try:
                    with get_db_conn() as conn:
                        record_node_outputs(conn, job_id, node_name, worker_id, outputs_to_record)
                except Exception as e:
                    logger.debug(f"Failed to record node outputs in artifact registry: {e}")
        elif status == "failed":
            if not error_message and exit_code == 137:
                error_message = f"OOMKilled: Stage '{node_name}' exceeded allocated memory and was killed by system OOM Killer (Exit code 137)"
            elif exit_code == 137 and "OOMKilled" not in (error_message or ""):
                error_message = f"OOMKilled: {error_message} (Exit code 137)"

            is_host_memory_pressure = (
                failure_reason == "HostMemoryPressureExceeded"
                or "HostMemoryPressureExceeded" in str(error_message)
            )
            is_pkg_verification_failure = (
                failure_reason == "PackageVerificationFailed"
                or "PackageVerificationFailed" in str(error_message)
                or "Fail-fast package verification failed" in str(error_message)
                or "FAIL-FAST:" in str(error_message)
            )
            is_non_retryable = is_host_memory_pressure or is_pkg_verification_failure

            max_retries = int(os.environ.get("CLUSTER_CI_MAX_NODE_RETRIES", "2"))

            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT retry_count FROM job_nodes WHERE job_id = ? AND node_name = ?', (job_id, node_name))
                row = cursor.fetchone()
                current_retries = row[0] if row and row[0] is not None else 0

            if not is_non_retryable and current_retries < max_retries:
                new_retry_count = current_retries + 1
                logger.info(
                    "🔄 Retrying node '%s' for job %s (retry %d/%d)",
                    node_name, job_id, new_retry_count, max_retries
                )
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    cas_json = json.dumps(cas_transfers) if cas_transfers is not None else "[]"
                    cursor.execute('''
                        UPDATE job_nodes
                        SET status = 'ready', retry_count = ?, worker_id = NULL, runner_id = NULL, gpu_ids = '[]',
                            duration_s = ?, exit_code = ?, error_message = ?, failure_reason = ?, cas_transfers = ?
                        WHERE job_id = ? AND node_name = ?
                    ''', (
                        new_retry_count, duration_s, exit_code, error_message,
                        failure_reason or f"retry_{new_retry_count}", cas_json,
                        job_id, node_name
                    ))
                    conn.commit()
                update_dag_ready_states(job_id)
            else:
                if is_host_memory_pressure:
                    final_reason = "HostMemoryPressureExceeded"
                elif is_pkg_verification_failure:
                    final_reason = "PackageVerificationFailed"
                else:
                    final_reason = failure_reason or "retries_exhausted"
                mark_node_status(
                    job_id, node_name, "failed",
                    duration_s=duration_s, exit_code=exit_code or 1,
                    error_message=error_message,
                    failure_reason=final_reason,
                    cas_transfers=cas_transfers
                )

            if error_message:
                try:
                    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    log_dir = os.path.join(repo_root, "job_logs")
                    os.makedirs(log_dir, exist_ok=True)
                    log_file = os.path.join(log_dir, f"{job_id}.log")
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(f"\n[CLUSTER-CI ERROR] Node '{node_name}' failed: {error_message}\n")
                except Exception as e:
                    logger.debug(f"Could not append node error to job log: {e}")
        elif status == "missing_deps":
            res = handle_missing_deps(job_id, node_name, missing_paths)
            if not res.get("success"):
                err = f"Missing deps could not be recovered: {missing_paths}"
                mark_node_status(job_id, node_name, "failed", exit_code=1,
                                 error_message=err)
                try:
                    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    log_dir = os.path.join(repo_root, "job_logs")
                    os.makedirs(log_dir, exist_ok=True)
                    log_file = os.path.join(log_dir, f"{job_id}.log")
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(f"\n[CLUSTER-CI ERROR] Node '{node_name}' failed: {err}\n")
                except Exception:
                    pass

    # Récupérer l'état du job et du worker
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM jobs WHERE job_id = ?', (job_id,))
        job_row = cursor.fetchone()
        if not job_row:
            return {"action": "finish", "error": "Job not found"}
        job = dict(job_row)

        cursor.execute('SELECT * FROM workers WHERE worker_id = ? OR hostname = ?', (worker_id, worker_id))
        worker_row = cursor.fetchone()
        if not worker_row:
            return {"action": "finish", "error": "Worker not found"}
        worker = dict(worker_row)
        worker_id = worker["worker_id"]

        # Récupérer les workers inactifs disponibles actuellement (pour vérifier si une machine réellement inactive peut accueillir un concurrent)
        cursor.execute('''
            SELECT * FROM workers
            WHERE status = 'online'
            AND last_seen >= datetime('now', '-60 seconds')
            AND worker_id != ?
            AND worker_id NOT IN (
                SELECT worker_id FROM jobs WHERE status IN ('running', 'assigned') AND worker_id IS NOT NULL
            )
            AND worker_id NOT IN (
                SELECT home_worker FROM jobs WHERE status IN ('running', 'assigned') AND home_worker IS NOT NULL
            )
            AND (assigned_job_id IS NULL OR assigned_job_id = '')
            AND worker_id NOT IN (
                SELECT worker_id FROM job_nodes WHERE status = 'running' AND worker_id IS NOT NULL
            )
        ''', (worker_id,))
        raw_idle = [dict(r) for r in cursor.fetchall()]

        cursor.execute("SELECT active_workers FROM jobs WHERE status IN ('running', 'assigned')")
        all_active_lists = [json.loads(r[0] or "[]") for r in cursor.fetchall()]
        all_active_worker_ids = set()
        for al in all_active_lists:
            all_active_worker_ids.update(al)

        idle_other_workers = [w for w in raw_idle if w["worker_id"] not in all_active_worker_ids]

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

    # 3. Sélection du prochain nœud intra-job avec comptabilité fine des ressources (Packing A11)
    allocated = get_worker_allocated_resources(conn, worker_id, exclude_node=(job_id, node_name))
    ready_nodes = [n for n in job_nodes if n["status"] == "ready"]
    admissible_ready_nodes = []
    for n in ready_nodes:
        n_res = json.loads(n["resources"]) if n["resources"] else {}
        n_res.setdefault("job_id", job_id)
        if is_worker_admissible_for_node(worker, n_res, allocated=allocated):
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
    assigned_node = None
    assigned_res = None
    assigned_gpu_ids = None

    for candidate_node, candidate_res in admissible_ready_nodes:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            fresh_allocated = get_worker_allocated_resources(conn, worker_id, exclude_node=(job_id, node_name))
            candidate_res.setdefault("job_id", job_id)
            if not is_worker_admissible_for_node(worker, candidate_res, allocated=fresh_allocated):
                conn.rollback()
                continue
            cand_gpu_ids = allocate_gpus(
                worker, candidate_res,
                allocated_gpu_ids=fresh_allocated.get("allocated_gpu_ids"),
                allocated_vram_by_gpu=fresh_allocated.get("allocated_vram_by_gpu")
            )
            req_g = int(candidate_res.get("gpus") if candidate_res.get("gpus") is not None else 0)
            req_v = float(candidate_res.get("vram_gb") or 0.0)
            if (req_g > 0 or req_v > 0) and cand_gpu_ids is None:
                conn.rollback()
                continue
            cursor.execute('''
                UPDATE job_nodes
                SET status = 'running', worker_id = ?, runner_id = ?, gpu_ids = ?, started_at = CURRENT_TIMESTAMP,
                    attempt = attempt + 1
                WHERE job_id = ? AND node_name = ? AND status = 'ready'
            ''', (worker_id, runner_id, json.dumps(cand_gpu_ids) if cand_gpu_ids is not None else '[]', job_id, candidate_node["node_name"]))
            if cursor.rowcount == 1:
                cursor.execute('SELECT attempt FROM job_nodes WHERE job_id = ? AND node_name = ?', (job_id, candidate_node["node_name"]))
                att_row = cursor.fetchone()
                assigned_attempt = att_row[0] if att_row else 1
                cursor.execute('''
                    UPDATE jobs
                    SET status = 'running', started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
                    WHERE job_id = ? AND status IN ('pending', 'assigned')
                ''', (job_id,))
                conn.commit()
                assigned_node = candidate_node
                assigned_res = candidate_res
                assigned_gpu_ids = cand_gpu_ids
                break
            else:
                conn.rollback()

    if assigned_node is None:
        if is_home:
            return {"action": "wait"}
        else:
            _release_worker_from_job(job_id, worker_id)
            return {"action": "yield"}

    next_node = assigned_node
    next_res = assigned_res
    gpu_ids = assigned_gpu_ids

    next_image = next_node.get("image") or next_res.get("image") or DEFAULT_DOCKER_IMAGE
    action = "run" if (current_image and next_image == current_image) else "switch_image"

    if runner_id:
        record_runner_heartbeat(job_id, runner_id, worker_id, next_node["node_name"])

    dep_paths_list = []
    raw_deps = next_node.get("dep_paths")
    if raw_deps:
        try:
            dep_paths_list = json.loads(raw_deps) if isinstance(raw_deps, str) else list(raw_deps)
        except Exception:
            dep_paths_list = []

    out_paths_list = []
    raw_outs = next_node.get("out_paths")
    if raw_outs:
        try:
            out_paths_list = json.loads(raw_outs) if isinstance(raw_outs, str) else list(raw_outs)
        except Exception:
            out_paths_list = []

    dep_sources_map = {}
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
                        online_workers_map[w_id] = normalize_worker_url(target_url)
                    except Exception:
                        online_workers_map[w_id] = target_url

            from src.scheduler.artifact_registry import hashes_for_paths
            local_consumer = bool(job.get('is_local'))
            dep_hashes = hashes_for_paths(conn, dep_paths_list, is_local=local_consumer)
            dep_sources_map = sources_for(conn, dep_hashes, online_workers_map,
                                         is_local=local_consumer)
    except Exception as e:
        logger.debug(f"Failed to query artifact sources: {e}")

    if gpu_ids is not None:
        next_res["gpu_ids"] = gpu_ids

    job_env_vars = {}
    try:
        raw_env = job.get("env_vars")
        if raw_env:
            job_env_vars = json.loads(raw_env) if isinstance(raw_env, str) else dict(raw_env)
    except Exception:
        job_env_vars = {}

    return {
        "action": action,
        "node": next_node["node_name"],
        "image": next_image,
        "resources": next_res,
        "gpu_ids": gpu_ids,
        "dep_paths": dep_paths_list,
        "dep_sources": dep_sources_map,
        "sources": dep_sources_map,
        "out_paths": out_paths_list,
        "attempt": assigned_attempt,
        "env_vars": job_env_vars,
    }

def check_resource_impossibility(resources, workers, item_name="job", is_classic=False):
    """
    Règle unique et factorisée de détection des jobs et nœuds impossibles (A17).
    Un job ou un nœud est impossible si AUCUNE machine enregistrée ne peut l'accueillir, même vide.
    Utilise rigoureusement la règle d'admission factorisée : is_worker_admissible_for_node(w, resources, allocated=None).
    
    Retourne : (is_impossible: bool, message: str)
    """
    if not workers:
        return False, ""

    # Test d'admissibilité universelle à vide
    if any(is_worker_admissible_for_node(w, resources, allocated=None) for w in workers):
        return False, ""

    # Aucune machine n'est admissible même à vide -> Calcul du diagnostic A17
    req_ram = float(resources.get("ram_gb") if resources.get("ram_gb") is not None else DEFAULT_RAM_GB)
    req_vram = float(resources.get("vram_gb") if resources.get("vram_gb") is not None else DEFAULT_VRAM_GB)
    req_cpus = int(resources.get("cpus") if resources.get("cpus") is not None else DEFAULT_CPUS)
    req_gpus = int(resources.get("gpus") if resources.get("gpus") is not None else (1 if req_vram > 0 else 0))
    req_storage = float(resources.get("storage_gb") or 0.0)

    worker_caps_desc = []
    unified_workers = []
    discrete_workers = []

    for w in workers:
        w_id = w.get("worker_id") or w.get("hostname") or "?"
        tot_ram = float(w.get("total_ram_gb") or 0.0)
        w_cpus = int(w.get("cpus") or 0)
        if is_unified_memory(w):
            avail_unified = max(0.0, tot_ram - OS_HEADROOM_GB)
            unified_workers.append((w_id, avail_unified, w_cpus))
            worker_caps_desc.append(f"machine {w_id} ({avail_unified:.1f} GB max unified RAM+VRAM, {w_cpus} CPUs)")
        else:
            avail_ram = max(0.0, tot_ram - 2.0)
            gpus = parse_vram_per_gpu(w)
            gpu_str = f"{len(gpus)} GPU(s) (max VRAM {max(gpus or [0.0]):.1f} GB)" if gpus else "0 GPU"
            discrete_workers.append((w_id, avail_ram, gpus, w_cpus))
            worker_caps_desc.append(f"machine {w_id} (max RAM {avail_ram:.1f} GB, {gpu_str}, {w_cpus} CPUs)")

    max_caps_str = " ; ".join(worker_caps_desc) if worker_caps_desc else "no machine online"
    max_unif_threshold = int(max([avail for _, avail, _ in unified_workers], default=113))

    if is_classic:
        max_cluster_gpus = max([get_worker_total_gpus(w) for w in workers], default=0)
        if req_gpus > max_cluster_gpus:
            demande_str = f"requests {req_gpus} GPUs"
            err_msg = (
                f"Classic job {item_name} impossible: {demande_str}; "
                f"maximum machine capacities: {max_caps_str}; "
                f"remedy: decrease requested GPUs to <= {max_cluster_gpus}"
            )
            return True, err_msg
        if req_vram > 0:
            demande_str = (
                f"requests REQUIRED_RAM={req_ram:.1f} GB and REQUIRED_VRAM={req_vram:.1f} GB "
                f"(sum={req_ram + req_vram:.1f} GB on unified memory)"
            )
        else:
            demande_str = f"requests REQUIRED_RAM={req_ram:.1f} GB"

        err_msg = (
            f"Classic job {item_name} impossible: {demande_str}; "
            f"maximum machine capacities: {max_caps_str}; "
            f"remedy: decrease REQUIRED_RAM + REQUIRED_VRAM to <= {max_unif_threshold} GB for GB10, "
            f"or declare meta.cluster per stage and enable v3 mode"
        )
        return True, err_msg
    else:
        # Nœud v3
        if req_vram > 0 and unified_workers and (req_ram + req_vram > max([avail for _, avail, _ in unified_workers], default=0)):
            err_msg = (
                f"node {item_name} requests ram_gb={req_ram:.1f} GB + vram_gb={req_vram:.1f} GB "
                f"({req_ram + req_vram:.1f} GB on unified memory); "
                f"maximum machine capacities: {max_caps_str}; "
                f"remedy: decrease ram_gb + vram_gb to <= {max_unif_threshold} GB for GB10, "
                f"or declare meta.cluster per stage and enable v3 mode"
            )
            return True, err_msg

        res_key = "ram_gb"
        req_val = f"{req_ram} GB"
        max_worker = "none"
        max_val = "0"

        max_w_ram = max(workers, key=lambda w: float(w.get("total_ram_gb") or 0.0), default=None)
        avail_ram = (float(max_w_ram.get("total_ram_gb") or 0.0) - (OS_HEADROOM_GB if is_unified_memory(max_w_ram) else 2.0)) if max_w_ram else 0.0
        if req_ram > avail_ram:
            res_key = "ram_gb"
            req_val = f"{req_ram} GB"
            max_worker = max_w_ram.get("worker_id") if max_w_ram else "none"
            max_val = f"{avail_ram:.1f} GB"
        elif req_gpus > 0:
            max_w_gpu = max(workers, key=lambda w: get_worker_total_gpus(w), default=None)
            avail_gpus = get_worker_total_gpus(max_w_gpu) if max_w_gpu else 0
            if req_gpus > avail_gpus:
                res_key = "gpus"
                req_val = req_gpus
                max_worker = max_w_gpu.get("worker_id") if max_w_gpu else "none"
                max_val = f"{avail_gpus} GPU"
            elif req_vram > 0:
                all_vrams = [max(parse_vram_per_gpu(w) or [0.0]) for w in workers]
                max_vram = max(all_vrams) if all_vrams else 0.0
                if req_vram > max_vram:
                    res_key = "vram_gb"
                    req_val = f"{req_vram} GB"
                    idx_max = all_vrams.index(max_vram) if all_vrams else 0
                    max_worker = workers[idx_max].get("worker_id") if workers else "none"
                    max_val = f"{max_vram:.1f} GB"
        elif req_cpus > 0:
            max_w_cpu = max(workers, key=lambda w: int(w.get("cpus") or 0), default=None)
            avail_cpus = int(max_w_cpu.get("cpus") or 0) if max_w_cpu else 0
            if req_cpus > avail_cpus:
                res_key = "cpus"
                req_val = req_cpus
                max_worker = max_w_cpu.get("worker_id") if max_w_cpu else "none"
                max_val = f"{avail_cpus} CPUs"
        elif req_storage > 0:
            max_w_stor = max(workers, key=lambda w: float(w.get("total_storage_gb") or 0.0), default=None)
            avail_stor = float(max_w_stor.get("total_storage_gb") or 0.0) if max_w_stor else 0.0
            if req_storage > avail_stor:
                res_key = "storage_gb"
                req_val = f"{req_storage} GB"
                max_worker = max_w_stor.get("worker_id") if max_w_stor else "none"
                max_val = f"{avail_stor:.1f} GB"

        err_msg = (
            f"node {item_name} requests {res_key}={req_val}; largest capacity: machine {max_worker} ({max_val}); "
            f"remedy: reduce meta.cluster.{res_key} of stage {item_name} in dvc.yaml"
        )
        return True, err_msg

def check_job_impossible_nodes(job_id, conn, workers):
    """
    Amendement A16 / A17 :
    Vérifie si un nœud du job ne peut être admis par AUCUNE machine du cluster, même vide.
    Si oui, fait échouer le job immédiatement avec message actionnable (cause + remède).
    """
    cursor = conn.cursor()
    cursor.execute('SELECT node_name, resources FROM job_nodes WHERE job_id = ? AND status IN ("pending", "ready")', (job_id,))
    nodes = cursor.fetchall()
    if not nodes:
        return False

    for row in nodes:
        n_name = row["node_name"]
        n_res = json.loads(row["resources"]) if row["resources"] else {}
        is_impossible, err_msg = check_resource_impossibility(n_res, workers, item_name=n_name, is_classic=False)
        if is_impossible:
            logger.error(f"Impossible node for job {job_id}: {err_msg}")
            cursor.execute('''
                UPDATE job_nodes SET status = 'failed', error_message = ?, finished_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND node_name = ?
            ''', (err_msg, job_id, n_name))
            cursor.execute('''
                UPDATE jobs SET status = 'failed', exit_code = 1, error_message = ?, finished_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
            ''', (err_msg, job_id))
            conn.commit()

            try:
                repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                log_dir = os.path.join(repo_root, "job_logs")
                os.makedirs(log_dir, exist_ok=True)
                log_file = os.path.join(log_dir, f"{job_id}.log")
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n[CLUSTER-CI ERROR] {err_msg}\n")
            except Exception:
                pass

            return True

    return False

def schedule_iteration():
    """
    Exécute une itération complète de planification avec PACKING (A11) et sélection A13 :
    1. Nettoyage préliminaire
    2. Gestion barrière de maintenance
    3. Ordonnancement des jobs parallèles v3 (home worker + exécuteurs supplémentaires)
    4. Ordonnancement des jobs classiques empilables avec nœuds v3
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
                now_utc = dt.datetime.now(dt.timezone.utc)
                elapsed_seconds = (now_utc.replace(tzinfo=None) - start_t).total_seconds()
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

        # Workers en ligne (Packing A11 : les workers ne sont pas exclus s'ils exécutent déjà des jobs)
        cursor.execute('''
            SELECT * FROM workers
            WHERE status = "online"
            AND last_seen >= datetime('now', '-60 seconds')
            ORDER BY placement_priority DESC, total_ram_gb DESC
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

    # Pré-calculer la comptabilité des ressources par worker (Packing A11)
    allocated_map = {}
    with get_db_conn() as conn:
        for w in workers:
            allocated_map[w["worker_id"]] = get_worker_allocated_resources(conn, w["worker_id"])

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

    for p_job in list(parallel_jobs):
        jid = p_job["job_id"]
        with get_db_conn() as conn:
            if check_job_impossible_nodes(jid, conn, workers):
                parallel_jobs.remove(p_job)
                continue

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
                cursor.execute('SELECT resources, dep_paths, image FROM job_nodes WHERE job_id = ? AND status = "ready"', (jid,))
                ready_rows = cursor.fetchall()

            admissible_candidates = []
            with get_db_conn() as conn:
                for w in workers:
                    w_alloc = allocated_map[w["worker_id"]]
                    for rr in ready_rows:
                        r_res = json.loads(rr["resources"]) if rr["resources"] else {}
                        r_res.setdefault("job_id", jid)
                        if is_worker_admissible_for_node(w, r_res, allocated=w_alloc):
                            raw_dp = rr["dep_paths"]
                            dp_list = json.loads(raw_dp) if raw_dp else []
                            node_img = rr["image"] or r_res.get("image")
                            admissible_candidates.append((w, r_res, dp_list, node_img))
                            break

            if admissible_candidates:
                # Choix A13 : Machines non-headnode d'abord (rang host_guard), puis coût minimal, puis la moins chargée
                with get_db_conn() as conn:
                    best_w, best_res, best_dp, best_img = max(
                        admissible_candidates,
                        key=lambda item: worker_selection_sort_key(
                            conn, item[0], allocated_map[item[0]["worker_id"]],
                            required_image=item[3], dep_paths=item[2]
                        )
                    )
                hw = best_w["worker_id"]
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

                p_job["home_worker"] = hw
                # Mettre à jour les ressources allouées pour le packing
                allocated_map[hw]["used_cpus"] += int(best_res.get("cpus") or DEFAULT_CPUS)
                allocated_map[hw]["used_ram_gb"] += float(best_res.get("ram_gb") if best_res.get("ram_gb") is not None else DEFAULT_RAM_GB)
                allocated_map[hw]["used_vram_gb"] += float(best_res.get("vram_gb") if best_res.get("vram_gb") is not None else DEFAULT_VRAM_GB)
                allocated_map[hw]["active_executors"] += 1
                allocated_map[hw].setdefault("active_job_ids", set()).add(jid)

    # 3.2 Garantie 1ère machine pour les jobs classiques en attente (Amendement A2 + Packing A11)
    # Les jobs classiques entrent dans la même comptabilité comme un nœud unique et sont empilables
    busy_home_or_classic = set()
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT home_worker FROM jobs WHERE status IN ('assigned', 'running') AND home_worker IS NOT NULL")
        for r in cursor.fetchall():
            if r[0]: busy_home_or_classic.add(r[0])
        cursor.execute("SELECT worker_id FROM jobs WHERE status IN ('assigned', 'running') AND (parallel_mode = 0 OR parallel_mode IS NULL) AND worker_id IS NOT NULL")
        for r in cursor.fetchall():
            if r[0]: busy_home_or_classic.add(r[0])

    classic_pending = [j for j in pending_jobs if not j.get('parallel_mode')]
    for c_job in list(classic_pending):
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

        c_res = {
            "ram_gb": ram_required,
            "vram_gb": vram_required,
            "cpus": DEFAULT_CPUS,
            "storage_gb": 0.0,
            "workers": allowed_workers
        }

        candidates = []
        for w in workers:
            w_alloc = allocated_map[w["worker_id"]]
            if is_worker_admissible_for_node(w, c_res, allocated=w_alloc):
                candidates.append(w)

        if not candidates:
            is_impossible, err_msg = check_resource_impossibility(c_res, workers, item_name=c_id, is_classic=True)
            if is_impossible:
                logger.error(f"Impossible classic job {c_id}: {err_msg}")
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE jobs SET status = 'failed', exit_code = 1, error_message = ?, finished_at = CURRENT_TIMESTAMP WHERE job_id = ?", (err_msg, c_id))
                    conn.commit()
                try:
                    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    log_dir = os.path.join(repo_root, "job_logs")
                    os.makedirs(log_dir, exist_ok=True)
                    log_file = os.path.join(log_dir, f"{c_id}.log")
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(f"\n[CLUSTER-CI ERROR] {err_msg}\n")
                except Exception:
                    pass
            continue

        # Data Locality (P2P Discovery) pour les jobs classiques
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

        worker_scores.sort(
            key=lambda x: (
                get_worker_placement_priority(x[0]),
                1 if x[0]['worker_id'] not in busy_home_or_classic else 0,
                x[1],
                -allocated_map[x[0]["worker_id"]]["active_executors"],
                float(x[0].get('total_ram_gb') or 0.0)
            ),
            reverse=True
        )
        assigned_worker, winner_score = worker_scores[0]

        p2p_url = None
        if winner_score < len(required_hashes) and len(worker_scores) > 1:
            peers = [ws for ws in worker_scores if ws[0]['worker_id'] != assigned_worker['worker_id']]
            if peers and peers[0][1] > 0 and peers[0][0].get('service_url'):
                p2p_url = f"{peers[0][0]['service_url']}/fetch_artifact".replace("1300.223.169.200", "130.223.169.200")

        c_gpu_ids = allocate_gpus(
            assigned_worker, c_res,
            allocated_gpu_ids=allocated_map[assigned_worker["worker_id"]].get("allocated_gpu_ids"),
            allocated_vram_by_gpu=allocated_map[assigned_worker["worker_id"]].get("allocated_vram_by_gpu")
        )
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE jobs
                SET status = 'assigned', worker_id = ?, p2p_url = ?, gpu_ids = ?
                WHERE job_id = ? AND status = 'pending'
            ''', (assigned_worker['worker_id'], p2p_url, json.dumps(c_gpu_ids) if c_gpu_ids is not None else '[]', c_id))
            if cursor.rowcount > 0:
                conn.commit()
                classic_pending.remove(c_job)
                allocated_map[assigned_worker["worker_id"]]["used_cpus"] += DEFAULT_CPUS
                allocated_map[assigned_worker["worker_id"]]["used_ram_gb"] += float(ram_required or DEFAULT_RAM_GB)
                allocated_map[assigned_worker["worker_id"]]["used_vram_gb"] += float(vram_required or 0.0)
                allocated_map[assigned_worker["worker_id"]]["active_executors"] += 1
                if c_gpu_ids:
                    for gid in c_gpu_ids:
                        allocated_map[assigned_worker["worker_id"]]["allocated_gpu_ids"].add(gid)
                busy_home_or_classic.add(assigned_worker["worker_id"])

    # 3.3 Répartition équitable des machines supplémentaires aux jobs parallèles (Packing A11 + W11)
    # Les machines supplémentaires sont allouées en priorité aux jobs ayant le moins de machines.
    # Le headnode n'est attribué comme machine supplémentaire que si aucune autre machine ne convient.
    # Les workers déjà assignés comme home_worker ou pour un job classique ne sont pas redistribués comme machines supplémentaires.
    busy_home_or_classic = set()
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT home_worker FROM jobs WHERE status IN ('assigned', 'running') AND home_worker IS NOT NULL")
        for r in cursor.fetchall():
            if r[0]: busy_home_or_classic.add(r[0])
        cursor.execute("SELECT worker_id FROM jobs WHERE status IN ('assigned', 'running') AND (parallel_mode = 0 OR parallel_mode IS NULL) AND worker_id IS NOT NULL")
        for r in cursor.fetchall():
            if r[0]: busy_home_or_classic.add(r[0])

    already_assigned_extra = set()
    for act_list in job_active_map.values():
        for wid in act_list:
            already_assigned_extra.add(wid)

    available_extra_workers = [
        w for w in workers
        if w["worker_id"] not in busy_home_or_classic and w["worker_id"] not in already_assigned_extra
    ]
    available_extra_workers.sort(key=lambda w: get_worker_placement_priority(w), reverse=True)

    for w in list(available_extra_workers):
        eligible_jobs = []
        for p_job in parallel_jobs:
            jid = p_job["job_id"]
            current_active = job_active_map.get(jid, [])
            if len(current_active) >= MAX_WORKERS_PER_JOB:
                continue

            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM job_nodes WHERE job_id = ? AND status IN ("ready", "running")', (jid,))
                parallelizable_count = cursor.fetchone()[0]
                if len(current_active) >= parallelizable_count:
                    continue

                cursor.execute('SELECT resources, dep_paths FROM job_nodes WHERE job_id = ? AND status = "ready"', (jid,))
                ready_rows = cursor.fetchall()

            w_alloc = allocated_map[w["worker_id"]]
            can_run_any = any(
                is_worker_admissible_for_node(
                    w,
                    dict(json.loads(rr["resources"]) if rr["resources"] else {}, job_id=jid),
                    allocated=w_alloc
                )
                for rr in ready_rows
            )
            if can_run_any:
                eligible_jobs.append((p_job, current_active))

        if eligible_jobs:
            # Règle d'équité A1 : Le job ayant le MOINS de machines actives reçoit la machine en priorité
            best_job, cur_act = min(
                eligible_jobs,
                key=lambda item: (
                    len(item[1]),
                    get_oldest_ready_node_time(item[0]),
                    -get_data_affinity_score(item[0], w)
                )
            )
            jid = best_job["job_id"]
            wid = w["worker_id"]
            if wid not in cur_act:
                cur_act.append(wid)
            job_active_map[jid] = cur_act
            allocated_map[wid].setdefault("active_job_ids", set()).add(jid)
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE jobs SET active_workers = ? WHERE job_id = ?',
                               (json.dumps(cur_act), jid))
                cursor.execute('UPDATE workers SET assigned_job_id = ? WHERE worker_id = ?', (jid, wid))
                conn.commit()
            available_extra_workers.remove(w)

def schedule_jobs():
    """Boucle continue d'ordonnancement cadencée toutes les 5 secondes."""
    last_retention_ts = 0.0
    while True:
        try:
            schedule_iteration()
        except Exception as e:
            logger.error(f"Error in scheduler loop: {e}")
        try:
            last_retention_ts, report = run_retention_periodic(last_retention_ts)
            if report and report.jobs_purged > 0:
                logger.info(f"Database retention: purged {report.jobs_purged} pathological jobs")
        except Exception as e:
            logger.error(f"Error in retention periodic: {e}")
        time.sleep(5)

if __name__ == '__main__':
    init_db()
    schedule_jobs()
