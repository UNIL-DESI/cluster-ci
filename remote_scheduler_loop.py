import time
import json
import requests
from persistence import get_db_conn, init_db
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def schedule_jobs():
    """
    Loop to assign PENDING jobs to available Workers using First-Fit (Bin-Packing).
    """
    while True:
        try:
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

                    # 2. Fetch pending jobs ordered by creation time
                    cursor.execute('SELECT * FROM jobs WHERE status = "pending" ORDER BY created_at ASC')
                    pending_jobs = [dict(row) for row in cursor.fetchall()]

                    if not pending_jobs:
                        time.sleep(5)
                        continue

                    # 2. Fetch online workers that are NOT already busy
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

                if not workers:
                    logger.warning("No online workers available.")
                    time.sleep(5)
                    continue

                for job in pending_jobs:
                    job_id = job['job_id']
                    ram_required = job['ram_required_gb']
                    repo = job['repo']
                    required_hashes = json.loads(job.get('required_hashes') or '[]')

                    # Hard Constraint: Filter workers by RAM
                    # Since workers are single-threaded and exclusively run one job at a time,
                    # they can use their full physical RAM minus a small OS overhead (2GB).
                    # We don't use 'available_ram_gb' because it is artificially lowered by ZFS ARC and reclaimable caches.
                    candidates = [w for w in workers if (w['total_ram_gb'] - 2.0) >= ram_required]

                    if not candidates:
                        # Check if it's fundamentally impossible by querying all online workers' total_ram_gb
                        with get_db_conn() as conn:
                            cursor = conn.cursor()
                            cursor.execute('SELECT MAX(total_ram_gb) FROM workers WHERE status = "online"')
                            max_total = cursor.fetchone()[0] or 0.0

                        if ram_required > (max_total - 2.0):
                            logger.error(f"Job {job_id} requires {ram_required} GB but max cluster capacity (minus 2GB OS overhead) is {max_total - 2.0:.1f} GB. Failing job.")
                            with get_db_conn() as conn:
                                cursor = conn.cursor()
                                cursor.execute("UPDATE jobs SET status = 'failed' WHERE job_id = ?", (job_id,))
                                conn.commit()
                            continue

                        logger.info(f"Could not find worker for job {job_id} requiring {ram_required} GB (waiting for a large enough worker to come online)")
                        continue

                    # Soft Constraint: Data Locality (P2P Discovery)
                    worker_scores = []
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
                        worker_scores.append((worker, score))

                    # Sort by score descending (Data Locality)
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
