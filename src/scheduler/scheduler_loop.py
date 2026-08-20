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
from persistence import get_db_conn, init_db
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def cancel_job_cleanly(job_id, exit_code=-15):
    """
    Cancels a job cleanly from the scheduler loop:
    - Contacts worker to kill containers (if assigned/running)
    - Cancels GH Action workflow (best effort)
    - Updates DB status to failed
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT j.*, w.service_url
            FROM jobs j
            LEFT JOIN workers w ON j.worker_id = w.worker_id
            WHERE j.job_id = ?
        ''', (job_id,))
        job = cursor.fetchone()

    if not job:
        return False

    status = job['status']
    if status not in ['pending', 'assigned', 'running']:
        return False

    # 1. Worker cancellation if active on worker
    if status in ['assigned', 'running'] and job['service_url']:
        try:
            requests.post(f"{job['service_url']}/cancel/{job_id}", timeout=10)
        except Exception as e:
            logger.error(f"Failed to send cancel to worker {job['service_url']} for job {job_id}: {e}")

    # 2. GHA cancellation (best effort)
    if job['gh_run_id']:
        try:
            repo = job['repo']
            run_id = job['gh_run_id']
            gh_token = job['gh_token'] or os.environ.get("GITHUB_PAT")
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

def schedule_jobs():
    """
    Loop to assign PENDING jobs to available Workers using First-Fit (Bin-Packing).
    Includes sovereign drainage barrier for maintenance jobs.
    """
    while True:
        try:
            expired_jobs = []
            pending_jobs = []
            workers = []

            with get_db_conn() as conn:
                cursor = conn.cursor()

                # 0. Ghost Workers cleanup: mark stale workers as offline
                # Workers send heartbeats every 10s. If we haven't heard from one
                # in 120s (12 missed heartbeats), it's dead/frozen.
                cursor.execute('''
                    UPDATE workers SET status = 'offline'
                    WHERE status = 'online' AND last_seen < datetime('now', '-120 seconds')
                ''')
                if cursor.rowcount > 0:
                    logger.warning(f"Marked {cursor.rowcount} ghost worker(s) as offline")
                conn.commit()

                # 1. Cleanup orphaned running/assigned jobs (workers that died/timed out)
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

                # 1.5. Sovereign Watchdog: Find running or assigned jobs exceeding max runtime
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
                        # SQLite timestamps are in UTC
                        start_t = datetime.strptime(start_time_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
                        now_utc = dt.datetime.utcnow()
                        elapsed_seconds = (now_utc - start_t).total_seconds()
                        limit_seconds = (max_hours * 3600) + 300  # plus 5 minutes grace margin
                        
                        if elapsed_seconds > limit_seconds:
                            logger.warning(f"Watchdog: Job {job_id} ({job['repo']}@{job['branch']}) has exceeded its max runtime of {max_hours} hours. (Elapsed: {elapsed_seconds/3600:.2f} hours)")
                            expired_jobs.append(job_id)
                    except Exception as ex:
                        logger.error(f"Watchdog parsing error for job {job_id}: {ex}")

                # Check if a maintenance job is currently RUNNING
                cursor.execute('''
                    SELECT * FROM jobs
                    WHERE status = 'running' AND (job_type = 'maintenance' OR is_maintenance = 1)
                    LIMIT 1
                ''')
                running_maintenance = cursor.fetchone()

                # 2. Fetch pending jobs ordered by priority (maintenance first, then FIFO)
                cursor.execute('''
                    SELECT * FROM jobs
                    WHERE status = "pending"
                    ORDER BY (CASE WHEN job_type = 'maintenance' OR is_maintenance = 1 THEN 0 ELSE 1 END) ASC, created_at ASC
                ''')
                pending_jobs = [dict(row) for row in cursor.fetchall()]

                # 3. Fetch online workers that are NOT already busy
                # Worker agents are single-threaded: they block in execute_job()
                # and cannot poll for new jobs until the current one finishes.
                # We must exclude workers that have a running or assigned job.
                cursor.execute('''
                    SELECT * FROM workers
                    WHERE status = "online"
                    AND last_seen >= datetime('now', '-60 seconds')
                    AND worker_id NOT IN (
                        SELECT worker_id FROM jobs
                        WHERE status IN ('running', 'assigned')
                        AND worker_id IS NOT NULL
                    )
                    ORDER BY total_ram_gb DESC
                ''')
                workers = [dict(row) for row in cursor.fetchall()]

            # Cancel expired jobs cleanly outside the main connection transaction to prevent SQLite locks
            for job_id in expired_jobs:
                try:
                    logger.warning(f"Watchdog: Forcefully cancelling expired job {job_id}")
                    cancel_job_cleanly(job_id, exit_code=-15)
                except Exception as e:
                    logger.error(f"Watchdog failed to cancel job {job_id}: {e}")

            if running_maintenance:
                logger.info(f"🚧 [MAINTENANCE] Maintenance job {running_maintenance['job_id']} is currently running. Normal scheduling paused.")
                time.sleep(5)
                continue

            if not pending_jobs:
                time.sleep(5)
                continue

            # Check if head of queue is a MAINTENANCE job (Drainage Barrier)
            head_job = pending_jobs[0]
            is_maintenance_job = (head_job.get('job_type') == 'maintenance' or head_job.get('is_maintenance') == 1)

            if is_maintenance_job:
                maint_job_id = head_job['job_id']
                # Check active compute jobs on cluster
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
                    logger.info(
                        f"🚧 [DRAINAGE BARRIER] Maintenance job {maint_job_id} holding queue: "
                        f"waiting for {active_compute_jobs} active compute job(s) to finish draining on nodes..."
                    )
                    # Freeze assignment of any new jobs
                    time.sleep(5)
                    continue

                # Drainage complete! All machines are idle (0 active compute jobs).
                logger.info(f"🚧 [DRAINAGE BARRIER] All nodes drained (0 active jobs). Switching maintenance job {maint_job_id} to RUNNING...")
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE jobs
                        SET status = 'running', started_at = CURRENT_TIMESTAMP
                        WHERE job_id = ? AND status = 'pending'
                    ''', (maint_job_id,))
                    conn.commit()

                # Trigger worker/cluster update orchestration
                success = orchestrate_cluster_update(head_job)
                
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    final_status = 'completed' if success else 'failed'
                    exit_code = 0 if success else 1
                    cursor.execute('''
                        UPDATE jobs
                        SET status = ?, exit_code = ?, finished_at = CURRENT_TIMESTAMP
                        WHERE job_id = ?
                    ''', (final_status, exit_code, maint_job_id))
                    conn.commit()
                
                logger.info(f"🏁 [MAINTENANCE] Maintenance job {maint_job_id} finished ({final_status}, exit code: {exit_code}). Resuming regular scheduler loop.")
                time.sleep(2)
                continue

            if not workers:
                logger.warning("No online workers available.")
                time.sleep(5)
                continue

            for job in pending_jobs:
                    job_id = job['job_id']
                    ram_required = job['ram_required_gb']
                    vram_required = job.get('vram_required_gb') or 0
                    repo = job['repo']
                    job_branch = job.get('branch', '')
                    required_hashes = json.loads(job.get('required_hashes') or '[]')

                    # Parse allowed_workers constraint
                    allowed_workers_raw = job.get('allowed_workers')
                    allowed_workers = json.loads(allowed_workers_raw) if allowed_workers_raw else None

                    # Branch-level exclusivity: never assign two jobs on the same repo+branch.
                    # If another job is already running/assigned, this one waits in the queue.
                    with get_db_conn() as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            SELECT COUNT(*) FROM jobs
                            WHERE repo = ? AND branch = ? AND status IN ('running', 'assigned')
                            AND job_id != ?
                        ''', (repo, job_branch, job_id))
                        if cursor.fetchone()[0] > 0:
                            logger.info(f"Branch exclusivity: skipping job {job_id} ({repo}@{job_branch}) — another job is already running/assigned on this branch")
                            continue

                    # Hard Constraint: Filter workers by RAM
                    # Since workers are single-threaded and exclusively run one job at a time,
                    # they can use their full physical RAM minus OS overhead (8GB).
                    # 8GB headroom protects the OS/Docker/worker-agent from OOM on unified
                    # memory systems (GB10/Grace) where CUDA allocations consume system RAM.
                    # We don't use 'available_ram_gb' because it is artificially lowered by ZFS ARC and reclaimable caches.
                    OS_HEADROOM_GB = 8.0
                    candidates = [w for w in workers if (w['total_ram_gb'] - OS_HEADROOM_GB) >= ram_required]

                    # Hard Constraint: Filter by VRAM (if required)
                    if vram_required > 0:
                        candidates = [w for w in candidates if (w.get('total_vram_gb') or 0) >= vram_required]

                    # Hard Constraint: Filter by ALLOWED_WORKERS (hostname whitelist)
                    if allowed_workers:
                        candidates = [w for w in candidates if w.get('hostname', '') in allowed_workers]

                    if not candidates:
                        # Check if it's fundamentally impossible by querying all online workers
                        with get_db_conn() as conn:
                            cursor = conn.cursor()
                            cursor.execute('SELECT MAX(total_ram_gb) FROM workers WHERE status = "online"')
                            max_total = cursor.fetchone()[0] or 0.0
                            cursor.execute('SELECT MAX(total_vram_gb) FROM workers WHERE status = "online"')
                            max_vram = cursor.fetchone()[0] or 0.0

                        if ram_required > (max_total - OS_HEADROOM_GB):
                            logger.error(f"Job {job_id} requires {ram_required} GB RAM but max cluster capacity is {max_total - OS_HEADROOM_GB:.1f} GB (total {max_total:.0f} GB - {OS_HEADROOM_GB:.0f} GB OS reserve). Failing job.")
                            with get_db_conn() as conn:
                                cursor = conn.cursor()
                                cursor.execute("UPDATE jobs SET status = 'failed' WHERE job_id = ?", (job_id,))
                                conn.commit()
                            continue

                        if vram_required > 0 and vram_required > max_vram:
                            logger.error(f"Job {job_id} requires {vram_required} GB VRAM but max cluster VRAM is {max_vram:.1f} GB. Failing job.")
                            with get_db_conn() as conn:
                                cursor = conn.cursor()
                                cursor.execute("UPDATE jobs SET status = 'failed' WHERE job_id = ?", (job_id,))
                                conn.commit()
                            continue

                        logger.info(f"Could not find worker for job {job_id} requiring {ram_required} GB RAM / {vram_required} GB VRAM")
                        continue

                    # Soft Constraint: Data Locality (P2P Discovery)
                    worker_scores = []
                    headnode_hostname = socket.gethostname()
                    for worker in candidates:
                        score = 0
                        if required_hashes and worker['service_url']:
                            try:
                                resp = requests.post(f"{worker['service_url']}/check_cache",
                                                     json={"repo": repo, "hashes": required_hashes},
                                                     timeout=2)
                                if resp.status_code == 200:
                                    found_hashes = resp.json()
                                    score = len(found_hashes)
                            except Exception as e:
                                logger.warning(f"Failed to check cache on worker {worker['worker_id']}: {e}")

                        # Headnode malus: deprioritize the headnode so remote workers
                        # are preferred at equal data locality scores
                        svc_url = worker.get('service_url') or ''
                        worker_hostname = worker.get('hostname', '')
                        is_headnode = (
                            worker_hostname == headnode_hostname
                            or 'localhost' in svc_url
                            or '127.0.0.1' in svc_url
                        )
                        if is_headnode:
                            score -= 1

                        worker_scores.append((worker, score))

                    # Sort by score descending (Data Locality, headnode deprioritized)
                    worker_scores.sort(key=lambda x: x[1], reverse=True)
                    assigned_worker, winner_score = worker_scores[0]

                    # Injection du Data Plane (P2P URL)
                    p2p_url = None
                    if winner_score < len(required_hashes) and len(worker_scores) > 1:
                        peers = [ws for ws in worker_scores if ws[0]['worker_id'] != assigned_worker['worker_id']]
                        if peers:
                            best_peer, peer_score = peers[0]
                            if peer_score > 0:
                                s_url = best_peer['service_url']
                                if s_url:
                                    s_url = s_url.replace("1300.223.169.200", "130.223.169.200")
                                p2p_url = f"{s_url}/fetch_artifact"

                    logger.info(f"Assigning job {job_id} to worker {assigned_worker['worker_id']} (Score: {winner_score}, P2P: {p2p_url})")

                    # Update Job status
                    with get_db_conn() as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            UPDATE jobs
                            SET status = 'assigned', worker_id = ?, p2p_url = ?
                            WHERE job_id = ? AND status = 'pending'
                        ''', (assigned_worker['worker_id'], p2p_url, job_id))

                        if cursor.rowcount > 0:
                            conn.commit()
                            # Mark worker as busy in-memory for subsequent jobs in this loop
                            workers = [w for w in workers if w['worker_id'] != assigned_worker['worker_id']]

        except Exception as e:
            logger.error(f"Error in scheduler loop: {e}")

        time.sleep(5)

if __name__ == '__main__':
    init_db()
    schedule_jobs()
