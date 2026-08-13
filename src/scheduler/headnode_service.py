import socket
# Force IPv4 to prevent infinite hangs on broken IPv6 networks (common on headless servers)
old_getaddrinfo = socket.getaddrinfo
def new_getaddrinfo(*args, **kwargs):
    responses = old_getaddrinfo(*args, **kwargs)
    return [response for response in responses if response[0] == socket.AF_INET]
socket.getaddrinfo = new_getaddrinfo
# Set a global timeout for all socket operations to prevent infinite hangs
socket.setdefaulttimeout(20.0)

from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, session, url_for, redirect, render_template, send_file
from persistence import init_db, get_db_conn
from authlib.integrations.flask_client import OAuth
import uuid
import datetime
import os
import shutil
import requests
import subprocess
import sys
import time
import threading
import re
import json
import yaml
import hashlib
import tempfile
import io
import base64
from urllib.parse import urlparse
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

# Helper to find executables
def get_executable(name):
    """Finds an executable in system PATH, local bin, or current venv."""
    cmd = shutil.which(name)
    if cmd: return cmd
    # Fallback to local user installation
    local_path = os.path.expanduser(f"~/.local/bin/{name}")
    if os.path.exists(local_path): return local_path
    # Fallback to virtual environment
    venv_path = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(venv_path): return venv_path
    return name

DVC_CMD = get_executable("dvc")
UV_CMD = get_executable("uv")

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB limit for source code archive uploads
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
# Derive a stable secret_key from CLUSTER_TOKEN so sessions survive service restarts.
# os.urandom(24) would invalidate all sessions on every restart.
_token = os.environ.get("CLUSTER_TOKEN", "")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or hashlib.sha256(f"flask-session-{_token}".encode()).digest()

oauth = OAuth(app)
oauth.register(
    name='github',
    client_id=os.environ.get('GITHUB_CLIENT_ID'),
    client_secret=os.environ.get('GITHUB_CLIENT_SECRET'),
    access_token_url='https://github.com/login/oauth/access_token',
    access_token_params=None,
    authorize_url='https://github.com/login/oauth/authorize',
    authorize_params=None,
    api_base_url='https://api.github.com/',
    client_kwargs={'scope': 'repo,user:email', 'timeout': 10.0},
)

FREE_SPACE_THRESHOLD_GB = 100
CLUSTER_TOKEN = os.environ.get("CLUSTER_TOKEN")
MAINTENANCE_MODE = False

def check_token():
    if not CLUSTER_TOKEN:
        return True # Default to no auth if not set
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    token = auth_header.split(" ")[1]
    return token == CLUSTER_TOKEN

@app.before_request
def require_token():
    # Only protect API endpoints that workers or users use to modify state
    protected_endpoints = ['register_worker', 'submit_job', 'update_job_status', 'worker_poll', 'notify_cleanup', 'maintenance_on', 'maintenance_off', 'download_code', 'sync_results']
    if request.endpoint in protected_endpoints:
        if not check_token():
            return jsonify({"error": "Unauthorized"}), 401

def cleanup_local_archive(job_id_or_path):
    """Clean up uploaded local source code archive after job completion or failure."""
    try:
        if isinstance(job_id_or_path, str) and os.path.exists(job_id_or_path) and job_id_or_path.endswith('.tar.gz'):
            os.remove(job_id_or_path)
            app.logger.info(f"🧹 Cleaned up local source archive: {job_id_or_path}")
        else:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT local_archive_path FROM jobs WHERE job_id = ?', (job_id_or_path,))
                row = cursor.fetchone()
                if row and row['local_archive_path'] and os.path.exists(row['local_archive_path']):
                    os.remove(row['local_archive_path'])
                    app.logger.info(f"🧹 Cleaned up local source archive for job {job_id_or_path}: {row['local_archive_path']}")
    except Exception as e:
        app.logger.warning(f"Failed to cleanup local archive for {job_id_or_path}: {e}")

@app.route('/maintenance/on', methods=['POST'])
def maintenance_on():
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = True
    return jsonify({"status": "ok", "maintenance": True})

@app.route('/maintenance/off', methods=['POST'])
def maintenance_off():
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = False
    return jsonify({"status": "ok", "maintenance": False})

@app.route('/register_worker', methods=['POST'])
def register_worker():
    data = request.json
    worker_id = data.get('worker_id')
    hostname = data.get('hostname')
    service_url = data.get('service_url')
    if service_url:
        service_url = service_url.replace("1300.223.169.200", "130.223.169.200")
    total_ram_gb = data.get('total_ram_gb')
    total_storage_gb = data.get('total_storage_gb')
    available_storage_gb = data.get('available_storage_gb')
    total_vram_gb = data.get('total_vram_gb')
    gpu_count = data.get('gpu_count')
    gpu_name = data.get('gpu_name')
    available_vram_gb = data.get('available_vram_gb', 0)

    with get_db_conn() as conn:
        cursor = conn.cursor()
        # available_ram_gb is now a derived state, but we keep the column for backward compatibility
        # (it will be ignored by the dynamic calculation).
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, available_ram_gb, total_storage_gb, available_storage_gb, total_vram_gb, gpu_count, gpu_name, available_vram_gb, last_seen, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'online')
            ON CONFLICT(worker_id) DO UPDATE SET
                hostname = ?,
                service_url = ?,
                total_ram_gb = ?,
                total_storage_gb = ?,
                available_storage_gb = ?,
                total_vram_gb = ?,
                gpu_count = ?,
                gpu_name = ?,
                available_vram_gb = ?,
                last_seen = CURRENT_TIMESTAMP,
                status = 'online'
        ''', (worker_id, hostname, service_url, total_ram_gb, total_ram_gb, total_storage_gb, available_storage_gb, total_vram_gb, gpu_count, gpu_name, available_vram_gb, hostname, service_url, total_ram_gb, total_storage_gb, available_storage_gb, total_vram_gb, gpu_count, gpu_name, available_vram_gb))
        
        # If a worker re-registers (is_startup=True), it means it restarted and lost any running jobs.
        # We only fail 'running' jobs. Jobs that were merely 'assigned' are safely reverted to 'pending'
        # so they can be rescheduled without falsely reporting a worker crash during assignment.
        is_startup = data.get('is_startup', False)
        if is_startup:
            cursor.execute('''
                UPDATE jobs
                SET status = 'failed', exit_code = COALESCE(exit_code, -98)
                WHERE worker_id = ? AND status = 'running'
            ''', (worker_id,))

            cursor.execute('''
                UPDATE jobs
                SET status = 'pending', worker_id = NULL
                WHERE worker_id = ? AND status = 'assigned'
            ''', (worker_id,))
        
        conn.commit()

    return jsonify({"status": "ok"})

def cancel_job_cleanly(job_id, exit_code=-15, reason="unspecified"):
    """
    Cancels a job cleanly:
    - If pending: updates DB status to failed, exit_code = exit_code.
    - If assigned or running: contacts worker to kill containers, cancels GH Action (best effort), updates DB status to failed.
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
        app.logger.warning(f"🔍 cancel_job_cleanly({job_id}): job not found in DB. reason={reason}")
        return False

    job = dict(job)  # Convert sqlite3.Row to dict for .get() support

    status = job['status']
    job_repo = job.get('repo', '?')
    job_branch = job.get('branch', '?')
    job_user = job.get('username', '?')
    app.logger.info(f"🛑 cancel_job_cleanly({job_id}): status={status}, repo={job_repo}, branch={job_branch}, user={job_user}, exit_code={exit_code}, reason={reason}")

    if status not in ['pending', 'assigned', 'running']:
        app.logger.info(f"🛑 cancel_job_cleanly({job_id}): skipped — status '{status}' is not cancellable")
        return False

    # 1. Worker cancellation if active on worker
    if status in ['assigned', 'running'] and job['service_url']:
        try:
            app.logger.info(f"🛑 cancel_job_cleanly({job_id}): sending /cancel to worker {job['service_url']}")
            requests.post(f"{job['service_url']}/cancel/{job_id}", timeout=10)
        except Exception as e:
            app.logger.error(f"Failed to send cancel to worker {job['service_url']} for job {job_id}: {e}")

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
                app.logger.info(f"🛑 cancel_job_cleanly({job_id}): cancelling GHA run {run_id} for {repo}")
                requests.post(gh_url, headers=headers, timeout=5)
        except Exception as e:
            app.logger.error(f"Failed to cancel GH Action for job {job_id}: {e}")

    # 3. Update DB
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE jobs
            SET status = 'failed', exit_code = ?, finished_at = CURRENT_TIMESTAMP
            WHERE job_id = ?
        ''', (exit_code, job_id))
        conn.commit()

    cleanup_local_archive(job_id)
    app.logger.info(f"🛑 cancel_job_cleanly({job_id}): done — job marked as failed")
    return True

@app.route('/submit_job', methods=['POST'])
def submit_job():
    if MAINTENANCE_MODE:
        return jsonify({"error": "Service Unavailable: Maintenance Mode Active"}), 503

    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form.to_dict() if request.form else {}

    is_local_val = data.get('is_local', False)
    is_local = bool(is_local_val and str(is_local_val).lower() in ['true', '1'])

    repo = data.get('repo')
    branch = data.get('branch')
    username = data.get('username')
    commit_hash = data.get('commit_hash')
    ram_required_gb = data.get('ram_required_gb', 0)
    max_runtime_hours = data.get('max_runtime_hours')
    exposed_port = data.get('exposed_port')
    vram_required_gb = data.get('vram_required_gb', 0)
    custom_web_app = data.get('custom_web_app', False)
    if isinstance(custom_web_app, str):
        custom_web_app = custom_web_app.lower() in ['true', '1']
    allowed_workers = data.get('allowed_workers')  # List of hostnames to restrict execution
    if isinstance(allowed_workers, str):
        try:
            allowed_workers = json.loads(allowed_workers)
        except Exception:
            allowed_workers = [w.strip() for w in allowed_workers.split(',') if w.strip()]
    gh_run_id = data.get('gh_run_id')
    gh_token = data.get('gh_token')
    env_vars = data.get('env_vars') # Dictionary of secrets
    if isinstance(env_vars, str):
        try:
            env_vars = json.loads(env_vars)
        except Exception:
            env_vars = None

    job_id = str(uuid.uuid4())
    local_archive_path = None

    if is_local:
        # Local job mode: assign local-draft/{username} branch and save source archive
        if username:
            branch = f"local-draft/{username}"
        elif not branch or not branch.startswith("local-draft/"):
            branch = "local-draft/anonymous"

        gh_run_id = None  # No GitHub Action associated with local jobs

        # Process source archive upload
        LOCAL_UPLOADS_DIR = os.path.join(REPOS_DIR, "_local_uploads")
        os.makedirs(LOCAL_UPLOADS_DIR, exist_ok=True)
        archive_dest = os.path.join(LOCAL_UPLOADS_DIR, f"{job_id}.tar.gz")

        archive_saved = False
        if 'archive' in request.files:
            archive_file = request.files['archive']
            archive_file.save(archive_dest)
            local_archive_path = archive_dest
            archive_saved = True
        elif data.get('source_archive'):
            with open(archive_dest, 'wb') as f:
                f.write(base64.b64decode(data['source_archive']))
            local_archive_path = archive_dest
            archive_saved = True

        if not archive_saved:
            app.logger.warning(f"Submit job {job_id} submitted with is_local=True but no archive file provided directly in submit_job request.")

        required_hashes = []
    else:
        # Standard GHA / Git mode: Shallow clone to get dvc.lock
        required_hashes = []
        repo_url = f"https://github.com/{repo}.git"

        # Prioritize token from submit_job (gh_token) over local GITHUB_PAT
        pat = gh_token or os.environ.get("GITHUB_PAT")

        if pat:
            repo_url = f"https://x-access-token:{pat}@github.com/{repo}.git"

        tmp_dir = tempfile.mkdtemp()
        try:
            # Shallow clone to get dvc.lock
            subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, "--no-checkout", repo_url, tmp_dir],
                           check=True, capture_output=True, timeout=30)
            # Checkout dvc.lock
            res = subprocess.run(["git", "checkout", "origin/" + branch, "--", "dvc.lock"],
                                 cwd=tmp_dir, capture_output=True, timeout=10)
            if res.returncode == 0:
                lock_path = os.path.join(tmp_dir, "dvc.lock")
                if os.path.exists(lock_path):
                    with open(lock_path, 'r') as f:
                        content = f.read()
                        # Extract MD5 hashes
                        required_hashes = list(set(re.findall(r'md5:\s*([a-f0-9]{32})', content)))
        except Exception as e:
            app.logger.error(f"Metadata extraction failed for {repo}@{branch}: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # 1. AUTO-CANCELLATION: Automatically cancel active jobs according to branch category
    # - Draft branches (cluster-draft/* or local-draft/*): Cancel active draft jobs
    #   for the same USERNAME — one active draft run per user at a time.
    # - Non-draft branches (main, feature/*, etc.): Only cancel PENDING jobs on the same repo+branch.
    jobs_to_cancel = []
    cancel_reasons = {}  # job_id -> reason string for tracing
    app.logger.info(f"📋 [AUTO-CANCEL] New submission: repo={repo}, branch={branch}, user={username}, is_local={is_local}")

    if branch:
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                if branch.startswith("local-draft/"):
                    if username:
                        cursor.execute('''
                            SELECT job_id, repo, branch, username, status FROM jobs
                            WHERE username = ? AND branch LIKE 'local-draft/%'
                            AND status IN ('pending', 'assigned', 'running')
                        ''', (username,))
                        active_jobs = cursor.fetchall()
                        app.logger.info(f"📋 [AUTO-CANCEL] Local draft mode: found {len(active_jobs)} active local draft job(s) for user={username}")
                        for aj in active_jobs:
                            aj_id = aj['job_id']
                            aj_repo = aj['repo'] or '?'
                            aj_status = aj['status']
                            jobs_to_cancel.append(aj_id)
                            cancel_reasons[aj_id] = f"local_draft:same_user (user={username}, repo={aj_repo}, status={aj_status})"
                elif branch.startswith("cluster-draft/"):
                    if username:
                        cursor.execute('''
                            SELECT job_id, repo, branch, username, status FROM jobs
                            WHERE username = ? AND branch LIKE 'cluster-draft/%'
                            AND status IN ('pending', 'assigned', 'running')
                        ''', (username,))
                        active_jobs = cursor.fetchall()
                        app.logger.info(f"📋 [AUTO-CANCEL] Cluster draft mode: found {len(active_jobs)} active draft job(s) for user={username} (cross-repo)")
                        for aj in active_jobs:
                            aj_id = aj['job_id']
                            aj_repo = aj['repo'] or '?'
                            aj_status = aj['status']
                            jobs_to_cancel.append(aj_id)
                            cancel_reasons[aj_id] = f"draft:same_user_cross_repo (user={username}, repo={aj_repo}, status={aj_status})"
                    else:
                        app.logger.warning("📋 [AUTO-CANCEL] Draft branch submitted without username — cannot perform cross-repo cancellation")
                else:
                    cursor.execute('''
                        SELECT job_id, branch, username, status FROM jobs
                        WHERE repo = ? AND branch = ? AND status = 'pending'
                    ''', (repo, branch))
                    active_jobs = cursor.fetchall()
                    app.logger.info(f"📋 [AUTO-CANCEL] Branch mode: found {len(active_jobs)} pending job(s) for {repo}@{branch}")
                    for aj in active_jobs:
                        aj_id = aj['job_id']
                        aj_status = aj['status']
                        jobs_to_cancel.append(aj_id)
                        cancel_reasons[aj_id] = f"branch:replace_pending (repo={repo}, branch={branch})"
        except Exception as e:
            app.logger.error(f"Error identifying active jobs to cancel: {e}")

    # Cancel identified jobs cleanly outside the active insertion transaction to prevent SQLite locks
    if jobs_to_cancel:
        cancel_details = ', '.join(f"{jid} ({cancel_reasons.get(jid, '?')})" for jid in jobs_to_cancel)
        app.logger.info(f"📋 [AUTO-CANCEL] Cancelling {len(jobs_to_cancel)} job(s): {cancel_details}")
    else:
        app.logger.info(f"📋 [AUTO-CANCEL] No jobs to cancel")
    for j_id in jobs_to_cancel:
        try:
            cancel_job_cleanly(j_id, exit_code=-15, reason=cancel_reasons.get(j_id, "auto-cancel"))
        except Exception as e:
            app.logger.error(f"Failed to auto-cancel job {j_id}: {e}")

    # Inject cancelled job IDs into env_vars for log notification
    if jobs_to_cancel:
        if not env_vars:
            env_vars = {}
        env_vars["CLUSTER_CANCELLED_RUNS"] = ",".join(jobs_to_cancel)

    # 2. Insert new job
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, commit_hash, ram_required_gb, vram_required_gb, max_runtime_hours, exposed_port, custom_web_app, gh_run_id, required_hashes, gh_token, env_vars, username, allowed_workers, status, is_local, local_archive_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ''', (job_id, repo, branch, commit_hash, ram_required_gb, vram_required_gb, max_runtime_hours, exposed_port, 1 if custom_web_app else 0, gh_run_id, json.dumps(required_hashes), gh_token, json.dumps(env_vars) if env_vars else None, username, json.dumps(allowed_workers) if allowed_workers else None, 1 if is_local else 0, local_archive_path))
        conn.commit()

    return jsonify({"job_id": job_id, "status": "pending", "required_hashes_count": len(required_hashes), "is_local": 1 if is_local else 0})

@app.route('/workers', methods=['GET'])
def list_workers():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        # available_ram_gb is now a derived state: Total - 2GB (OS margin) - Sum of RAM required by active jobs
        cursor.execute('''
            SELECT
                worker_id, hostname, service_url, total_ram_gb,
                (total_ram_gb - 2.0 - (
                    SELECT COALESCE(SUM(ram_required_gb), 0)
                    FROM jobs
                    WHERE worker_id = workers.worker_id AND status IN ('running', 'assigned')
                )) as available_ram_gb,
                total_storage_gb, available_storage_gb, total_vram_gb, available_vram_gb, gpu_count, gpu_name, last_seen, status
            FROM workers
        ''')
        workers = [dict(row) for row in cursor.fetchall()]
    return jsonify(workers)

@app.route('/scheduler_status', methods=['GET'])
def scheduler_status():
    """
    Exposes a detailed public view of the cluster scheduler state:
    - Active workers with their current running/assigned jobs.
    - Pending jobs queue with resource requirements and waiting times.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        
        # 1. Fetch all workers and dynamically attach any active job currently running or assigned
        cursor.execute('''
            SELECT worker_id, hostname, service_url, total_ram_gb, total_vram_gb, gpu_count, gpu_name, status, last_seen
            FROM workers
        ''')
        workers_list = [dict(row) for row in cursor.fetchall()]
        
        for w in workers_list:
            cursor.execute('''
                SELECT job_id, repo, branch, username, ram_required_gb, max_runtime_hours, status, created_at, started_at, is_local
                FROM jobs
                WHERE worker_id = ? AND status IN ('running', 'assigned')
                LIMIT 1
            ''', (w['worker_id'],))
            job = cursor.fetchone()
            if job:
                w['active_job'] = dict(job)
            else:
                w['active_job'] = None
                
        # 2. Fetch the pending queue ordered by FIFO priority
        cursor.execute('''
            SELECT job_id, repo, branch, username, ram_required_gb, status, created_at, is_local
            FROM jobs
            WHERE status = 'pending'
            ORDER BY created_at ASC
        ''')
        pending_queue = [dict(row) for row in cursor.fetchall()]
        
    return jsonify({
        "workers": workers_list,
        "queue": pending_queue
    })

@app.route('/job_status/<job_id>', methods=['GET'])
def job_status(job_id):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT j.*, w.service_url as worker_service_url
            FROM jobs j
            LEFT JOIN workers w ON j.worker_id = w.worker_id
            WHERE j.job_id = ?
        ''', (job_id,))
        job = cursor.fetchone()
        if job:
            return jsonify(dict(job))
        else:
            return jsonify({"error": "Job not found"}), 404

@app.route('/api/jobs/<job_id>/download_code', methods=['GET'])
def download_code(job_id):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT local_archive_path, is_local FROM jobs WHERE job_id = ?', (job_id,))
        job = cursor.fetchone()
    if not job or not job['is_local']:
        return jsonify({"error": "Not a local job"}), 404
    archive_path = job['local_archive_path']
    if not archive_path or not os.path.exists(archive_path):
        return jsonify({"error": "Archive not found"}), 404
    return send_file(
        archive_path,
        mimetype='application/gzip',
        as_attachment=True,
        download_name=f"{job_id}.tar.gz"
    )

@app.route('/api/jobs/<job_id>/sync_results', methods=['POST'])
def sync_results(job_id):
    LOCAL_RESULTS_DIR = os.path.join(REPOS_DIR, "_local_results")
    job_results_dir = os.path.join(LOCAL_RESULTS_DIR, job_id)
    os.makedirs(job_results_dir, exist_ok=True)

    if request.files:
        count = 0
        for key, file_obj in request.files.items():
            file_name = file_obj.filename or key
            clean_name = os.path.normpath(file_name).lstrip('/\\')
            if clean_name.startswith('..'):
                continue
            dest_path = os.path.join(job_results_dir, clean_name)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            file_obj.save(dest_path)
            count += 1
        return jsonify({"status": "ok", "synced_files": count})
    elif request.is_json:
        data = request.get_json() or {}
        files_dict = data.get("files", {})
        count = 0
        for rel_path, b64_content in files_dict.items():
            clean_name = os.path.normpath(rel_path).lstrip('/\\')
            if clean_name.startswith('..'):
                continue
            dest_path = os.path.join(job_results_dir, clean_name)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(base64.b64decode(b64_content))
            count += 1
        return jsonify({"status": "ok", "synced_files": count})
    else:
        return jsonify({"error": "No files or json payload provided"}), 400

@app.route('/api/jobs/<job_id>/results', methods=['GET'])
def get_job_results(job_id):
    LOCAL_RESULTS_DIR = os.path.join(REPOS_DIR, "_local_results")
    job_results_dir = os.path.join(LOCAL_RESULTS_DIR, job_id)
    zip_file_path = os.path.join(LOCAL_RESULTS_DIR, f"{job_id}.zip")

    if os.path.exists(zip_file_path):
        return send_file(
            zip_file_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"results_{job_id[:8]}.zip"
        )
    elif os.path.exists(job_results_dir) and os.listdir(job_results_dir):
        memory_file = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(job_results_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, job_results_dir)
                    zf.write(full_path, rel_path)
        memory_file.seek(0)
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"results_{job_id[:8]}.zip"
        )
    else:
        return jsonify({"error": "No results found for this job"}), 404

@app.route('/worker_poll/<worker_id>', methods=['GET'])
def worker_poll(worker_id):
    # This endpoint is for workers to check if they have a job assigned
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM jobs
            WHERE worker_id = ? AND status = 'assigned'
            ORDER BY created_at ASC LIMIT 1
        ''', (worker_id,))
        job = cursor.fetchone()
        if job:
            return jsonify(dict(job))
        else:
            return jsonify({"status": "no_job"})

@app.route('/update_job_status', methods=['POST'])
def update_job_status():
    data = request.json
    job_id = data.get('job_id')

    # Handle GHA detachment (non-draft branch workflow replacement).
    # When GHA cancels a workflow due to concurrency on a non-draft branch,
    # submit_job.py sends detach_gha=True instead of a full cancellation.
    # We clear gh_run_id so clean_ghosts won't kill the still-running worker job.
    if data.get('detach_gha'):
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE jobs SET gh_run_id = NULL WHERE job_id = ?', (job_id,))
            conn.commit()
        app.logger.info(f"🔗 GHA detached from job {job_id} (non-draft branch concurrency replacement — worker job continues)")
        return jsonify({"status": "ok", "message": "GHA run detached, worker job preserved"})

    status = data.get('status')
    exit_code = data.get('exit_code')
    commit_hash = data.get('commit_hash')
    viewer_port = data.get('viewer_port')

    with get_db_conn() as conn:
        cursor = conn.cursor()
        
        cursor.execute('SELECT status, worker_id, ram_required_gb FROM jobs WHERE job_id = ?', (job_id,))
        job = cursor.fetchone()
        if not job:
            return jsonify({"error": "Job not found"}), 404
            
        current_status = job['status']

        # If it's an external cancellation (indicated by negative exit code from signal propagation like GHA TERM)
        # and the job is currently assigned or running, we must route it via cancel_job_cleanly to notify the worker.
        if status == 'failed' and current_status in ['assigned', 'running'] and exit_code is not None and int(exit_code) < 0:
            conn.commit()  # Release current transaction before calling cancel_job_cleanly to avoid SQLite locks
            app.logger.info(f"🔄 Routing external cancellation signal ({exit_code}) for job {job_id} through cancel_job_cleanly")
            cancel_job_cleanly(job_id, exit_code=exit_code, reason=f"external_signal(exit_code={exit_code})")
            return jsonify({"status": "ok", "message": "Cancellation signal routed cleanly"})

        if status == 'running':
            cursor.execute('''
                UPDATE jobs SET
                    status = ?,
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    commit_hash = COALESCE(?, commit_hash),
                    viewer_port = COALESCE(?, viewer_port)
                WHERE job_id = ?
            ''', (status, commit_hash, viewer_port, job_id))
        elif status in ['completed', 'failed']:
            cursor.execute('UPDATE jobs SET status = ?, finished_at = CURRENT_TIMESTAMP, exit_code = COALESCE(?, exit_code), commit_hash = COALESCE(?, commit_hash) WHERE job_id = ?', (status, exit_code, commit_hash, job_id))
            cleanup_local_archive(job_id)
        else:
            cursor.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (status, job_id))
        conn.commit()
    return jsonify({"status": "ok"})

@app.route('/clean_ghosts', methods=['POST'])
def clean_ghosts():
    """Cleans up ghost jobs (jobs that are pending in DB but their GH workflow is completed/cancelled)"""
    # R1: Retrieve candidates outside of the active transaction loop to avoid SQLite lock contentions
    ghost_candidate_jobs = []
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT job_id, repo, gh_run_id FROM jobs WHERE status IN ('pending', 'running', 'assigned') AND gh_run_id IS NOT NULL")
        ghost_candidate_jobs = [dict(row) for row in cursor.fetchall()]
        
    cleaned = 0
    errors = []
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_PAT")
    headers = {"Authorization": f"token {gh_token}", "Accept": "application/vnd.github.v3+json"} if gh_token else {}
    
    for job in ghost_candidate_jobs:
        url = f"https://api.github.com/repos/{job['repo']}/actions/runs/{job['gh_run_id']}"
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            # R2: Fail-Fast (No Silent Fallback) on rate limiting or auth errors to expose infrastructure issues immediately
            if resp.status_code == 403:
                err_msg = f"❌ GitHub API returned 403 Forbidden for job {job['job_id']}. Rate limit exceeded or invalid GH_TOKEN/GITHUB_PAT."
                app.logger.error(err_msg)
                errors.append(RuntimeError(err_msg))
                continue
                
            if resp.status_code == 200:
                run_data = resp.json()
                status = run_data.get('status')
                conclusion = run_data.get('conclusion')
                if status == 'completed' or conclusion is not None:
                    app.logger.info(f"Ghost job detected: {job['job_id']} (GH status: {status}, conclusion: {conclusion}). Marking as failed & releasing worker resources.")
                    cancel_job_cleanly(job['job_id'], exit_code=-15, reason=f"ghost_cleanup(gh_status={status}, conclusion={conclusion})")
                    cleaned += 1
            elif resp.status_code == 404:
                app.logger.info(f"Ghost job detected (404): {job['job_id']}. Marking as failed & releasing worker resources.")
                cancel_job_cleanly(job['job_id'], exit_code=-15, reason="ghost_cleanup(gh_404)")
                cleaned += 1
            else:
                err_msg = f"Unexpected response status {resp.status_code} from GitHub API for job {job['job_id']}"
                app.logger.error(err_msg)
                errors.append(RuntimeError(err_msg))
        except Exception as e:
            app.logger.error(f"Error checking ghost job {job['job_id']}: {e}")
            errors.append(e)
            
    if errors:
        raise RuntimeError(f"Ghost job reconciliation completed with {len(errors)} error(s). Details: " + "; ".join([str(e) for e in errors]))
            
    return jsonify({"status": "ok", "cleaned_jobs": cleaned})

@app.route('/check_space', methods=['GET'])
def check_space():
    # Use the root of the repositories directory if it exists, else use current dir
    repo_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "repositories")
    if not os.path.exists(repo_dir):
        repo_dir = "."

    usage = shutil.disk_usage(repo_dir)
    free_gb = usage.free / (1024**3)

    return jsonify({
        "free_gb": free_gb,
        "threshold_gb": FREE_SPACE_THRESHOLD_GB,
        "sufficient": free_gb > FREE_SPACE_THRESHOLD_GB
    })

REPOS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "repositories")

def find_local_repo(repo_slug):
    """Find the local clone of a repo, handling owner name mismatches."""
    # Try exact match first
    exact = os.path.join(REPOS_DIR, repo_slug)
    if os.path.exists(exact) and os.path.exists(os.path.join(exact, ".git")):
        return exact
    
    # Fallback: search by repo name only across all owner dirs
    repo_name = repo_slug.split('/')[-1] if '/' in repo_slug else repo_slug
    if os.path.exists(REPOS_DIR):
        for owner_dir in os.listdir(REPOS_DIR):
            if owner_dir.startswith('_'):  # Skip _tmp_artifacts etc.
                continue
            candidate = os.path.join(REPOS_DIR, owner_dir, repo_name)
            if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, ".git")):
                return candidate
    return None

@app.route('/artifacts/<repo_owner>/<repo_name>/<rev>/<path:file_path>', methods=['GET'])
def artifacts(repo_owner, repo_name, rev, file_path):
    """
    Unified artifact access API — P2P First architecture.

    Strategy (in order):
      1. Proxy to the worker that ran the job for this exact revision (P2P)
      2. Proxy to any online worker that has run jobs for this repo (P2P)
      3. Fallback: local DVC extraction on headnode (requires remote storage)
    """
    repo_slug = f"{repo_owner}/{repo_name}"

    # --- Strategy 1 & 2: P2P Worker Proxy (Primary Path) ---
    # Workers have DVC caches from executing jobs — no remote storage needed.
    # Try ALL online workers: the DVC cache may only exist on one specific worker.
    workers_to_try = []
    with get_db_conn() as conn:
        cursor = conn.cursor()
        # Get all distinct online workers that have run jobs for this repo
        cursor.execute('''
            SELECT DISTINCT w.service_url
            FROM jobs j
            JOIN workers w ON j.worker_id = w.worker_id
            WHERE j.repo = ? AND w.status = 'online'
            ORDER BY j.finished_at DESC
        ''', (repo_slug,))
        workers_to_try = [row['service_url'] for row in cursor.fetchall() if row['service_url']]

    inline_param = "&inline=true" if request.args.get("inline") == "true" else ""
    for worker_url_base in workers_to_try:
        worker_url = f"{worker_url_base}/api/worker/dvc/get?repo={repo_slug}&rev={rev}&path={file_path}{inline_param}"
        app.logger.info(f"[P2P] Proxying artifact {file_path}@{rev} to worker {worker_url_base}")
        try:
            resp = proxy_request(worker_url)
            if resp.status_code < 400:
                return resp
            if resp.status_code >= 500:
                app.logger.warning(f"[P2P] Worker {worker_url_base} returned {resp.status_code}, trying next worker")
                continue
            # 404: file not on this worker's cache, try next
            app.logger.info(f"[P2P] Worker {worker_url_base} returned 404, trying next worker")
        except Exception as e:
            app.logger.warning(f"[P2P] Worker {worker_url_base} proxy failed: {e}, trying next worker")

    # --- Strategy 3: Local Headnode DVC Extraction (Last Resort) ---
    request_id = str(uuid.uuid4())
    tmp_dir = os.path.join(REPOS_DIR, "_tmp_artifacts", request_id)
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        local_repo_path = find_local_repo(repo_slug)
        if local_repo_path:
            pat = os.environ.get("GITHUB_PAT")
            if pat:
                new_url = f"https://x-access-token:{pat}@github.com/{repo_slug}.git"
                subprocess.run(["git", "remote", "set-url", "origin", new_url], cwd=local_repo_path)
            subprocess.run(["git", "fetch", "origin"], cwd=local_repo_path, capture_output=True, text=True)

        source = local_repo_path if local_repo_path else f"https://github.com/{repo_slug}"

        cmd = [DVC_CMD, "get", source, file_path, "--rev", rev, "--out", tmp_dir]
        result = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())

        if result.returncode == 0:
            filename = os.path.basename(file_path)
            full_path = os.path.join(tmp_dir, filename)

            import mimetypes
            mime_type, _ = mimetypes.guess_type(filename)
            if not mime_type:
                mime_type = 'application/octet-stream'

            disposition = "inline" if request.args.get("inline") == "true" else "attachment"

            def generate():
                try:
                    with open(full_path, 'rb') as f:
                        while True:
                            chunk = f.read(4096)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

            return Response(generate(), mimetype=mime_type,
                            headers={"Content-Disposition": f"{disposition}; filename=\"{filename}\""})

        shutil.rmtree(tmp_dir, ignore_errors=True)
        error_msg = result.stderr.strip() if result.stderr else "Unknown DVC error"
        return jsonify({"error": f"No worker available and local extraction failed: {error_msg}"}), 404

    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Internal error during extraction: {str(e)}"}), 500


@app.route('/notify_cleanup', methods=['POST'])
def notify_cleanup():
    # Fetch all online workers with a service_url
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT service_url FROM workers WHERE status = 'online' AND service_url IS NOT NULL")
        workers = cursor.fetchall()

    notified = 0
    errors = 0
    for worker in workers:
        service_url = worker['service_url']
        try:
            # Send drain request to each worker
            resp = requests.post(f"{service_url}/webhook/drain_request", timeout=5)
            if resp.status_code == 200:
                notified += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    return jsonify({"status": "ok", "notified": notified, "errors": errors})

# --- History & DVC Exploration APIs ---

@app.route('/api/projects', methods=['GET'])
def api_list_projects():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    token = session.get('token')
    target_config = os.environ.get("TARGET_REPO", "UNIL-DESI").lower()

    try:
        repos_resp = oauth.github.get('user/repos?per_page=100&sort=updated', token=token, timeout=15.0)
        if not repos_resp.ok:
            return jsonify({"error": "Failed to fetch repositories from GitHub", "details": repos_resp.text}), 502

        repos = repos_resp.json()
        if not isinstance(repos, list):
            return jsonify({"error": "Unexpected response from GitHub"}), 502

        allowed_repos = set()
        for r in repos:
            full_name = r['full_name'].lower()
            owner = r.get('owner', {}).get('login', '').lower()
            # User must have push permission
            if not r.get('permissions', {}).get('push', False):
                continue

            # Match against target organization OR specific repository
            if owner == target_config or full_name == target_config:
                allowed_repos.add(r['full_name'].lower())

        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT repo FROM jobs')
            projects_in_db = [row['repo'] for row in cursor.fetchall()]

        # Only return projects that are in the database AND the user has access to (case-insensitive)
        projects = [p for p in projects_in_db if p.lower() in allowed_repos]
        return jsonify(projects)
    except Exception as e:
        app.logger.error(f"Error fetching repos in API: {e}")
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

@app.route('/api/projects/<path:repo>/runs', methods=['GET'])
def api_list_runs(repo):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT job_id, branch, status, commit_hash, created_at, started_at, finished_at, exit_code, custom_web_app, is_local
            FROM jobs
            WHERE repo = ?
            ORDER BY created_at DESC
        ''', (repo,))
        runs = [dict(row) for row in cursor.fetchall()]

    local_repo_path = find_local_repo(repo)
    
    if local_repo_path:
        hashes = [run['commit_hash'] for run in runs if run.get('commit_hash')]
        if hashes:
            try:
                res = subprocess.run(
                    ["git", "--no-pager", "show", "-s", "--format=%H|%s"] + hashes,
                    cwd=local_repo_path,
                    capture_output=True,
                    text=True
                )
                title_map = {}
                for line in res.stdout.strip().split('\n'):
                    if '|' in line:
                        h, t = line.split('|', 1)
                        title_map[h] = t
                for run in runs:
                    run['commit_title'] = title_map.get(run.get('commit_hash'), "")
            except Exception:
                for run in runs: run['commit_title'] = ""
    else:
        for run in runs: run['commit_title'] = ""

    return jsonify(runs)
    
@app.route('/api/jobs/<job_id>/logs', methods=['GET'])
def api_get_run_logs(job_id):
    offset = request.args.get('offset', 0)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT w.service_url
            FROM jobs j
            JOIN workers w ON j.worker_id = w.worker_id
            WHERE j.job_id = ?
        ''', (job_id,))
        job = cursor.fetchone()
        
    if not job or not job['service_url']:
        return jsonify({"logs": "Log source not found (worker might be offline or job not assigned)", "offset": offset})
        
    worker_url = f"{job['service_url']}/job_logs/{job_id}?offset={offset}"
    try:
        resp = requests.get(worker_url, timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
        else:
            return jsonify({"logs": f"Error fetching logs from worker: {resp.text}", "offset": offset}), 500
    except Exception as e:
        return jsonify({"logs": f"Connection error to worker: {str(e)}", "offset": offset}), 500

@app.route('/api/runs/<job_id>/files', methods=['GET'])
def api_run_files(job_id):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT repo, commit_hash, branch FROM jobs WHERE job_id = ?', (job_id,))
        job = cursor.fetchone()

    if not job:
        return jsonify({"error": "Job not found"}), 404

    repo = job['repo']
    commit_hash = job['commit_hash']
    branch = job['branch'] or 'main'

    if not commit_hash:
        return jsonify({"error": "Commit hash not found"}), 400

    pat = os.environ.get("GITHUB_PAT")
    repo_url = f"https://x-access-token:{pat}@github.com/{repo}.git" if pat else f"https://github.com/{repo}.git"

    local_repo_path = find_local_repo(repo)

    # Support subfolder navigation via optional 'path' query parameter
    sub_path = request.args.get('path', '')

    def build_dvc_cmd(source, sub, rev):
        if sub:
            return [DVC_CMD, "list", source, sub, "--rev", rev, "--dvc-only", "--json"]
        return [DVC_CMD, "list", source, "--rev", rev, "--dvc-only", "--json"]

    try:
        env = os.environ.copy()

        # Strategy:
        # 1. Try local headnode repo
        # 2. Try proxying to a worker that ran this job
        # 3. Fallback to GitHub URL

        if local_repo_path:
            cmd = build_dvc_cmd(local_repo_path, sub_path, commit_hash)
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)

            if result.returncode != 0 and "unknown Git revision" in result.stderr:
                # Local repo is stale — fetch latest commits
                app.logger.info(f"Fetching latest commits for {repo} (revision {commit_hash[:8]} not found locally)")
                subprocess.run(["git", "fetch", "--all", "--prune"], cwd=local_repo_path,
                               capture_output=True, timeout=30)
                result = subprocess.run(cmd, capture_output=True, text=True, env=env)

            if result.returncode == 0:
                return Response(result.stdout, mimetype='application/json')

        # Strategy 2: Proxy to worker
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT w.service_url
                FROM jobs j
                JOIN workers w ON j.worker_id = w.worker_id
                WHERE j.job_id = ? AND w.status = 'online'
            ''', (job_id,))
            worker = cursor.fetchone()

        if worker and worker['service_url']:
            worker_url = f"{worker['service_url']}/api/worker/dvc/list?repo={repo}&rev={commit_hash}"
            app.logger.info(f"Proxying DVC list for job {job_id} to worker {worker['service_url']}")
            try:
                return proxy_request(worker_url)
            except Exception as e:
                app.logger.warning(f"Worker proxy failed for DVC list: {e}")

        # Fallback: use GitHub URL directly
        cmd = build_dvc_cmd(repo_url, sub_path, commit_hash)
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)

        if result.returncode != 0:
            return jsonify({
                "error": "Failed to list DVC files",
                "details": result.stderr
            }), 500

        return Response(result.stdout, mimetype='application/json')
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Portal & OAuth Routes ---

@app.route('/api/jobs/<job_id>/stop', methods=['POST'])
def api_stop_job(job_id):
    if 'user' not in session and not check_token():
        return jsonify({"error": "Unauthorized"}), 401

    success = cancel_job_cleanly(job_id, exit_code=-1)
    if success:
        return jsonify({"status": "ok", "message": "Job stopped and verified"})
    else:
        return jsonify({"error": "Job not found or not active"}), 404

@app.route('/api/runs/active', methods=['GET'])
def api_active_runs():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM jobs 
                WHERE status IN ('running', 'assigned')
                ORDER BY created_at DESC
            ''')
            runs = [dict(row) for row in cursor.fetchall()]
        return jsonify(runs)
    except Exception as e:
        app.logger.error(f"Error fetching active runs: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/queue', methods=['GET'])
def api_queue():
    """Returns pending jobs with computed wait reasons for the dashboard."""
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            # Fetch pending jobs
            cursor.execute('''
                SELECT job_id, repo, branch, username, ram_required_gb, status, created_at, is_local
                FROM jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
            ''')
            pending_jobs = [dict(row) for row in cursor.fetchall()]

            # Fetch running/assigned jobs for reason computation
            cursor.execute('''
                SELECT job_id, repo, branch, username, ram_required_gb, status, worker_id
                FROM jobs
                WHERE status IN ('running', 'assigned')
            ''')
            active_jobs = [dict(row) for row in cursor.fetchall()]

            # Fetch online workers
            cursor.execute('''
                SELECT worker_id, hostname, total_ram_gb, status
                FROM workers
                WHERE status = 'online'
            ''')
            workers = [dict(row) for row in cursor.fetchall()]

        # Compute wait reason for each pending job
        for job in pending_jobs:
            reasons = []
            job_repo = job['repo']
            job_branch = job['branch'] or ''

            # Check branch exclusivity (another job running/assigned on same repo+branch)
            branch_blocked = any(
                aj['repo'] == job_repo and aj['branch'] == job_branch
                for aj in active_jobs
            )
            if branch_blocked:
                reasons.append("branch_exclusivity")

            # Check resource availability (no worker with enough RAM)
            ram_required = job.get('ram_required_gb', 0)
            # Workers currently free (not running any active job)
            busy_worker_ids = {aj['worker_id'] for aj in active_jobs if aj.get('worker_id')}
            free_workers = [w for w in workers if w['worker_id'] not in busy_worker_ids]
            compatible_free = [w for w in free_workers if (w['total_ram_gb'] - 2.0) >= ram_required]

            if not compatible_free:
                if not free_workers:
                    reasons.append("no_free_workers")
                else:
                    reasons.append("insufficient_ram")

            job['wait_reasons'] = reasons if reasons else ["scheduling"]

        return jsonify(pending_jobs)
    except Exception as e:
        app.logger.error(f"Error fetching queue: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/')
def dashboard():
    if 'user' not in session:
        return render_template('login.html')

    return render_template('dashboard.html', user=session['user'])

@app.route('/login')
def login():
    redirect_uri = url_for('authorize', _external=True)
    print(f"DEBUG: Redirecting to GitHub. redirect_uri={redirect_uri}", flush=True)
    return oauth.github.authorize_redirect(redirect_uri)

@app.route('/authorize')
def authorize():
    print(f"DEBUG: /authorize reached. Args: {request.args}", flush=True)
    try:
        print("DEBUG: Fetching access token...", flush=True)
        token = oauth.github.authorize_access_token()
        print(f"DEBUG: Token received. Fetching user info...", flush=True)
        resp = oauth.github.get('user', token=token)
        user = resp.json()
        print(f"DEBUG: User info received: {user.get('login')}. Setting session...", flush=True)
        session['user'] = user
        session['token'] = token
        print("DEBUG: Redirecting to dashboard.", flush=True)
        return redirect(url_for('dashboard'))
    except Exception as e:
        print(f"DEBUG ERROR in /authorize: {str(e)}", flush=True)
        import traceback
        traceback.print_exc()
        return f"Authentication Error: {str(e)}", 500

@app.route('/logout')
def logout():
    session.pop('user', None)
    session.pop('token', None)
    return redirect(url_for('dashboard'))

# --- Proxy & DVC-Viewer Management ---

DVC_VIEWER_PORT = int(os.environ.get("DVC_VIEWER_PORT", 8686))
DVC_VIEWER_TIMEOUT_MIN = int(os.environ.get("DVC_VIEWER_TIMEOUT_MIN", 30))

# Registry for local dvc-viewer processes
# { repo_full_name: { 'proc': subprocess.Popen, 'port': int, 'last_access': float } }
local_viewers = {}
local_viewers_lock = threading.Lock()

# Registry for remote dvc-viewer processes on workers
# { repo_full_name: { 'worker_id': str, 'worker_url': str, 'port': int, 'last_access': float, 'rev': str } }
remote_viewers = {}
remote_viewers_lock = threading.Lock()

def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port

def cleanup_inactive_viewers():
    """Background task to kill local and remote inactive dvc-viewer processes."""
    while True:
        time.sleep(30)
        now = time.time()
        
        # 1. Cleanup local viewers
        to_delete_local = []
        with local_viewers_lock:
            for repo_name, viewer in local_viewers.items():
                if now - viewer['last_access'] > (DVC_VIEWER_TIMEOUT_MIN * 60):
                    print(f"Terminating inactive local dvc-viewer for {repo_name} (port {viewer['port']})")
                    try:
                        viewer['proc'].terminate()
                        viewer['proc'].wait(timeout=5)
                    except Exception as e:
                        print(f"Error terminating process: {e}")
                        try:
                            viewer['proc'].kill()
                        except:
                            pass
                    to_delete_local.append(repo_name)

            for repo_name in to_delete_local:
                del local_viewers[repo_name]
                
        # 2. Cleanup remote viewers (local metadata registry only, process self-destructs on worker)
        to_delete_remote = []
        with remote_viewers_lock:
            for repo_name, viewer in remote_viewers.items():
                # 45 seconds timeout is very safe since worker dvc-viewer self-destructs in 15 seconds of inactivity.
                if now - viewer['last_access'] > 45:
                    print(f"Removing inactive remote dvc-viewer registry entry for {repo_name} (worker {viewer['worker_url']}, port {viewer['port']})")
                    to_delete_remote.append(repo_name)
                    
            for repo_name in to_delete_remote:
                del remote_viewers[repo_name]

def periodic_clean_ghosts():
    """Background task to periodically clean ghost jobs."""
    while True:
        time.sleep(60)
        try:
            with app.app_context():
                clean_ghosts()
        except Exception as e:
            app.logger.error(f"Error in background clean_ghosts: {e}")

@app.route('/view/<owner>/<repo>/')
@app.route('/view/<owner>/<repo>/<path:path>')
def view_project(owner, repo, path=''):
    if 'user' not in session:
        return redirect(url_for('dashboard'), code=302)

    repo_full_name = f"{owner}/{repo}"

    # --- Case 1: Live (Running on a worker) ---
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT w.service_url, j.viewer_port
            FROM jobs j
            JOIN workers w ON j.worker_id = w.worker_id
            WHERE j.repo = ? AND j.status = 'running'
            ORDER BY j.started_at DESC LIMIT 1
        ''', (repo_full_name,))
        job = cursor.fetchone()

    if job and job['service_url']:
        worker_base_url = job['service_url']
        # Use dynamic port if available, otherwise fallback to default
        viewer_port = job['viewer_port'] if ('viewer_port' in job.keys() and job['viewer_port'] is not None) else DVC_VIEWER_PORT
        # Extract hostname/IP from service_url (e.g., http://worker1:6000 -> worker1)
        parsed = urlparse(worker_base_url)
        target_host = parsed.hostname
        target_url = f"http://{target_host}:{viewer_port}/{path}"
        base_href = f"/view/{owner}/{repo}/" if path == '' else None

        result = proxy_request(target_url, base_href=base_href)
        # Check if proxy_request returned an error tuple (message, status_code)
        if isinstance(result, tuple) and len(result) == 2 and result[1] == 502:
            # Smart Proxy: App Booting View with Real-time Logs
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT job_id FROM jobs WHERE repo = ? AND status = "running" ORDER BY started_at DESC LIMIT 1', (repo_full_name,))
                job_row = cursor.fetchone()
                job_id = job_row['job_id'] if job_row else "unknown"

            return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Workspace — Booting...</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f1f5f9;
       display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; overflow: hidden; }}
.container {{ background: #1e293b; border-radius: 16px; padding: 2rem; width: 90%; max-width: 900px; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5); border: 1px solid #334155; }}
.header {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 1.5rem; }}
.spinner {{ width: 24px; height: 24px; border: 3px solid #38bdf8; border-top-color: transparent; border-radius: 50%; animation: spin 1s linear infinite; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
h1 {{ font-size: 1.25rem; margin: 0; color: #38bdf8; }}
.logs-container {{ background: #020617; border-radius: 8px; padding: 1rem; font-family: "Fira Code", "Cascadia Code", monospace; font-size: 0.85rem; height: 400px; overflow-y: auto; border: 1px solid #1e293b; position: relative; }}
.logs-content {{ white-space: pre-wrap; color: #94a3b8; }}
.status-bar {{ margin-top: 1rem; font-size: 0.75rem; color: #64748b; display: flex; justify-content: space-between; }}
</style></head>
<body><div class="container">
    <div class="header">
        <div class="spinner"></div>
        <h1>🚀 Workspace Booting...</h1>
    </div>
    <p style="margin-top: 0; color: #94a3b8; font-size: 0.9rem;">Your application is starting on the worker. Please wait while we set up the environment.</p>
    <div class="logs-container" id="log-scroll">
        <div class="logs-content" id="logs">Connecting to log stream...</div>
    </div>
    <div class="status-bar">
        <span>Worker: {target_host}:{viewer_port}</span>
        <span id="poll-status">Polling for 200 OK...</span>
    </div>
</div>
<script>
    const logElement = document.getElementById('logs');
    const scrollElement = document.getElementById('log-scroll');
    const statusElement = document.getElementById('poll-status');
    let offset = 0;
    const basePath = '/view/{owner}/{repo}/';

    async function pollLogs() {{
        try {{
            const resp = await fetch(`/api/jobs/{job_id}/logs?offset=${{offset}}`);
            const data = await resp.json();
            if (data.logs) {{
                if (offset === 0) logElement.textContent = '';
                logElement.textContent += data.logs;
                offset = data.offset;
                scrollElement.scrollTop = scrollElement.scrollHeight;
            }}
        }} catch (e) {{ console.error("Log fetch error", e); }}
    }}

    async function sendHeartbeat() {{
        // Keep the dvc-viewer alive by sending heartbeats through the proxy.
        // Without this, the viewer's inactivity daemon would kill it before
        // any real client connects.
        try {{
            await fetch(basePath + 'api/heartbeat', {{ cache: 'no-store' }});
        }} catch (e) {{ /* viewer not ready yet, ignore */ }}
    }}

    async function checkApp() {{
        try {{
            // Use the heartbeat endpoint as a lightweight health check.
            // This avoids the full HTML proxy + session/redirect issues of HEAD requests.
            const resp = await fetch(basePath + 'api/heartbeat', {{ cache: 'no-store' }});
            if (resp.ok) {{
                statusElement.textContent = "✅ App is Ready! Redirecting...";
                statusElement.style.color = "#4ade80";
                setTimeout(() => window.location.href = basePath, 500);
                return;
            }}
        }} catch (e) {{ }}
        setTimeout(checkApp, 2000);
    }}

    setInterval(pollLogs, 2000);
    setInterval(sendHeartbeat, 5000);
    pollLogs();
    sendHeartbeat();
    checkApp();
</script>
</body></html>""", 502
        return result

    # --- Case 2: Historical (Remote Worker) ---
    rev = request.args.get('rev')
    # When no rev is specified, the worker will fetch latest and use origin/main.
    # This ensures we always show the latest state, not a stale job commit.

    with remote_viewers_lock:
        if repo_full_name in remote_viewers:
            viewer = remote_viewers[repo_full_name]
            if viewer.get('rev') == rev:
                viewer['last_access'] = time.time()
                parsed = urlparse(viewer['worker_url'])
                target_host = parsed.hostname
                target_url = f"http://{target_host}:{viewer['port']}/{path}"
                base_href = f"/view/{owner}/{repo}/" if path == '' else None
                
                res = proxy_request(target_url, base_href=base_href)
                if isinstance(res, tuple) and len(res) == 2 and res[1] == 502:
                    app.logger.warning(f"Remote viewer for {repo_full_name} on {viewer['worker_url']} is unreachable. Will recreate.")
                else:
                    return res

            if repo_full_name in remote_viewers:
                del remote_viewers[repo_full_name]

        # Smart worker selection based on cache locality
        with get_db_conn() as conn:
            cursor = conn.cursor()
            # 1. Try to find the last online worker that ran a job for this repo
            cursor.execute('''
                SELECT w.worker_id, w.service_url
                FROM jobs j
                JOIN workers w ON j.worker_id = w.worker_id
                WHERE j.repo = ? AND w.status = 'online' AND w.service_url IS NOT NULL
                ORDER BY j.finished_at DESC LIMIT 1
            ''', (repo_full_name,))
            worker = cursor.fetchone()

            # 2. Otherwise, fall back to any online worker
            if not worker:
                cursor.execute('''
                    SELECT worker_id, service_url
                    FROM workers
                    WHERE status = 'online' AND service_url IS NOT NULL
                    LIMIT 1
                ''')
                worker = cursor.fetchone()

        if not worker:
            return "No online worker available to host the historical visualizer. Please start a worker first.", 503

        worker_id = worker['worker_id']
        worker_url = worker['service_url']

        app.logger.info(f"Requesting worker {worker_url} to start historical dvc-viewer for {repo_full_name} at revision {rev}")
        try:
            resp = requests.post(
                f"{worker_url}/api/worker/dvc-viewer/start",
                json={"repo": repo_full_name, "rev": rev},
                timeout=60
            )
            if resp.status_code != 200:
                return f"Failed to start historical dvc-viewer on worker: {resp.text}", 502
            
            data_resp = resp.json()
            port = data_resp.get('port')
            if not port:
                return "Failed to start historical dvc-viewer: worker did not return an access port", 500

            remote_viewers[repo_full_name] = {
                'worker_id': worker_id,
                'worker_url': worker_url,
                'port': port,
                'last_access': time.time(),
                'rev': rev
            }

            parsed = urlparse(worker_url)
            target_host = parsed.hostname
            target_url = f"http://{target_host}:{port}/{path}"
            base_href = f"/view/{owner}/{repo}/" if path == '' else None
            return proxy_request(target_url, base_href=base_href)

        except Exception as e:
            app.logger.error(f"Error calling worker to start dvc-viewer: {e}")
            return f"Failed to reach worker to start dvc-viewer: {str(e)}", 502

def proxy_request(target_url, base_href=None):
    """Simple proxy that forwards the request to the target_url.

    Args:
        target_url: The URL to forward the request to.
        base_href: If set, inject a <base href="..."> tag into HTML responses.
                   This fixes relative URL resolution when the viewer is served
                   behind a reverse proxy at a sub-path.
    """
    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers={key: value for (key, value) in request.headers if key != 'Host'},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            params=request.args,
            stream=True,
            timeout=10
        )

        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for (name, value) in resp.raw.headers.items()
                   if name.lower() not in excluded_headers]

        # Rewrite absolute paths to relative paths in HTML responses
        content_type = resp.headers.get('content-type', '')
        if 'text/html' in content_type:
            body = resp.content.decode('utf-8', errors='replace')
            # The <base href> tag is useless for absolute paths (starting with /).
            # Instead, we directly rewrite the absolute paths in the HTML to relative paths.
            body = body.replace('"/api/', '"api/')
            body = body.replace("'/api/", "'api/")
            body = body.replace('"/static/', '"static/')
            body = body.replace("'/static/", "'static/")
            if base_href:
                body = body.replace('<head>', f'<head><base href="{base_href}">', 1)
            response = Response(body, status=resp.status_code, headers=headers)
            response.headers['Content-Type'] = content_type
            return response

        response = Response(stream_with_context(resp.iter_content(chunk_size=1024)),
                            status=resp.status_code,
                            headers=headers)
        return response
    except Exception as e:
        return f"Proxy Error: {str(e)}", 502

@app.route('/api/projects/<path:repo>/run/<commit>/hydra-params', methods=['GET'])
def api_hydra_params(repo, commit):
    """Extract Hydra/YAML config parameters from dvc.yaml deps at a specific revision.

    Scans dvc.yaml stages for .yaml/.yml dependency files, groups them by
    config file, and returns their parsed content. This uses the deps declared
    by researchers rather than guessing the Hydra directory structure.
    """
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    local_repo_path = find_local_repo(repo)
    if not local_repo_path:
        return jsonify({"error": "Repository not found on headnode"}), 404

    try:
        # 1. Get dvc.yaml at this commit
        res = subprocess.run(
            ["git", "show", f"{commit}:dvc.yaml"],
            cwd=local_repo_path, capture_output=True, text=True, timeout=5
        )
        if res.returncode != 0:
            return jsonify({"configs": []})

        dvc_data = yaml.safe_load(res.stdout) or {}
        stages = dvc_data.get("stages", {})

        # 2. Collect all YAML deps across all stages
        yaml_deps = {}  # path -> set of stage names that reference it
        for stage_name, stage_def in stages.items():
            if not isinstance(stage_def, dict):
                continue

            deps_list = stage_def.get("deps", [])
            # Handle foreach/do pattern
            do_block = stage_def.get("do", {})
            if isinstance(do_block, dict):
                deps_list = (deps_list or []) + (do_block.get("deps", []) or [])

            if not deps_list:
                continue

            for dep in deps_list:
                dep_path = dep if isinstance(dep, str) else (list(dep.keys())[0] if isinstance(dep, dict) else None)
                if not dep_path:
                    continue
                # Skip template variables, hash files, and non-YAML deps
                if "${" in dep_path or ".dvc-viewer/" in dep_path:
                    continue
                if dep_path.endswith(('.yaml', '.yml')):
                    if dep_path not in yaml_deps:
                        yaml_deps[dep_path] = set()
                    yaml_deps[dep_path].add(stage_name)

        # 3. Fetch content of each YAML config at the commit
        configs = []
        for config_path, stage_names in sorted(yaml_deps.items()):
            try:
                res_cfg = subprocess.run(
                    ["git", "show", f"{commit}:{config_path}"],
                    cwd=local_repo_path, capture_output=True, text=True, timeout=5
                )
                if res_cfg.returncode == 0 and res_cfg.stdout.strip():
                    parsed = yaml.safe_load(res_cfg.stdout)
                    configs.append({
                        "path": config_path,
                        "stages": sorted(stage_names),
                        "content": parsed if parsed else {}
                    })
            except Exception:
                continue

        return jsonify({"configs": configs})

    except Exception as e:
        app.logger.error(f"Error fetching Hydra params for {repo}@{commit}: {e}")
        return jsonify({"error": str(e)}), 500

def extract_metrics_and_plots_paths(dvc_yaml_data):
    """Extract metrics/plots paths from dvc.yaml, with stage name and type info.
    
    Returns:
        path_info: dict mapping path -> {"stage": str, "type": "metric"|"plot"}
        pattern_info: list of (compiled_regex, stage_name, type_str)
    """
    path_info = {}       # path -> {"stage": stage_name, "type": "metric"|"plot"}
    pattern_info = []    # [(compiled_regex, stage_name, type_str)]
    
    def resolve_item(item, stage_name, artifact_type):
        if not item:
            return
        if isinstance(item, list):
            for x in item:
                resolve_item(x, stage_name, artifact_type)
        elif isinstance(item, dict):
            for k in item.keys():
                if isinstance(k, str):
                    add_path(k, stage_name, artifact_type)
        elif isinstance(item, str):
            add_path(item, stage_name, artifact_type)
            
    def add_path(p, stage_name, artifact_type):
        if "${" in p:
            # Transform to regex pattern
            # e.g., "artifacts/metrics-${item}.json" -> "^artifacts/metrics-.*\.json$"
            pattern_str = re.escape(p)
            pattern_str = re.sub(r'\\\$\\\{[^}]+\\\}', '.*', pattern_str)
            try:
                pattern_info.append((re.compile(f"^{pattern_str}$"), stage_name, artifact_type))
            except Exception:
                pass
        else:
            path_info[p] = {"stage": stage_name, "type": artifact_type}

    if isinstance(dvc_yaml_data, dict):
        stages = dvc_yaml_data.get("stages", {})
        if isinstance(stages, dict):
            for stage_name, stage_def in stages.items():
                if not isinstance(stage_def, dict):
                    continue
                
                # Check metrics & plots in stage
                for art_type, key in [("metric", "metrics"), ("plot", "plots")]:
                    resolve_item(stage_def.get(key), stage_name, art_type)
                
                # Check if it is a foreach / do
                do_block = stage_def.get("do", {})
                if isinstance(do_block, dict):
                    for art_type, key in [("metric", "metrics"), ("plot", "plots")]:
                        resolve_item(do_block.get(key), stage_name, art_type)
                    
        # DVC 1.0 or other styles might have top level plots/metrics
        for art_type, key in [("metric", "metrics"), ("plot", "plots")]:
            resolve_item(dvc_yaml_data.get(key), "_top_level", art_type)
        
    return path_info, pattern_info

def is_matching_artifact(file_path, path_info, pattern_info):
    """Check if file_path is a declared artifact. Returns (stage, type) or None."""
    if file_path in path_info:
        info = path_info[file_path]
        return info["stage"], info["type"]
    for pat, stage_name, art_type in pattern_info:
        if pat.match(file_path):
            return stage_name, art_type
    return None


def extract_artifacts_from_lock(dvc_lock_data, dvc_yaml_data):
    """Extract all metrics/plots artifacts from dvc.lock using dvc.yaml for type classification.
    
    dvc.lock contains the resolved paths (no ${item} variables) for each stage.
    dvc.yaml contains the template patterns that tell us if a path is a metric or plot.
    
    Returns:
        list of dicts: [{"path": str, "stage": str, "artifact_type": "metric"|"plot"}, ...]
    """
    # 1. Build type classifier from dvc.yaml (metrics vs plots patterns)
    path_info, pattern_info = extract_metrics_and_plots_paths(dvc_yaml_data)
    
    # 2. Also build a set of all metrics/plots paths declared in dvc.yaml 
    #    for stages WITHOUT foreach (these have no ${} and are exact paths)
    declared_metrics_paths = set()
    declared_plots_paths = set()
    if isinstance(dvc_yaml_data, dict):
        stages = dvc_yaml_data.get("stages", {})
        if isinstance(stages, dict):
            for stage_name, stage_def in stages.items():
                if not isinstance(stage_def, dict):
                    continue
                # Direct stage metrics/plots
                for paths_set, key in [(declared_metrics_paths, "metrics"), (declared_plots_paths, "plots")]:
                    _collect_paths(stage_def.get(key), paths_set)
                # foreach/do block
                do_block = stage_def.get("do", {})
                if isinstance(do_block, dict):
                    for paths_set, key in [(declared_metrics_paths, "metrics"), (declared_plots_paths, "plots")]:
                        _collect_paths(do_block.get(key), paths_set)

    # 3. Extract all outs from dvc.lock stages
    artifacts = []
    seen_paths = set()
    
    if not isinstance(dvc_lock_data, dict):
        return artifacts
        
    lock_stages = dvc_lock_data.get("stages", dvc_lock_data)
    if not isinstance(lock_stages, dict):
        return artifacts
    
    for stage_name, stage_def in lock_stages.items():
        if not isinstance(stage_def, dict):
            continue
            
        outs = stage_def.get("outs", [])
        if not isinstance(outs, list):
            continue
            
        for out_entry in outs:
            if not isinstance(out_entry, dict):
                continue
            file_path = out_entry.get("path")
            if not file_path or file_path in seen_paths:
                continue
            
            # Classify: is this path a metric or plot?
            artifact_type = _classify_artifact_type(
                file_path, path_info, pattern_info,
                declared_metrics_paths, declared_plots_paths
            )
            
            if artifact_type:
                # Clean stage name: "step_foo@tomplay" -> "step_foo"
                clean_stage = stage_name.split("@")[0] if "@" in stage_name else stage_name
                artifacts.append({
                    "path": file_path,
                    "stage": clean_stage,
                    "artifact_type": artifact_type
                })
                seen_paths.add(file_path)
    
    return artifacts

def _collect_paths(item, paths_set):
    """Recursively collect all path strings from a metrics/plots declaration."""
    if not item:
        return
    if isinstance(item, list):
        for x in item:
            _collect_paths(x, paths_set)
    elif isinstance(item, dict):
        for k in item.keys():
            if isinstance(k, str):
                paths_set.add(k)
    elif isinstance(item, str):
        paths_set.add(item)

def _classify_artifact_type(file_path, path_info, pattern_info,
                            declared_metrics_paths, declared_plots_paths):
    """Determine if a file is a metric, plot, or not an artifact.
    
    Uses multiple strategies:
    1. Exact match in path_info (non-foreach paths from dvc.yaml)
    2. Regex match in pattern_info (foreach paths from dvc.yaml)  
    3. Check if the path matches any declared metric/plot template after variable resolution
    
    Returns: "metric", "plot", or None
    """
    # Strategy 1: Exact match from dvc.yaml (non-foreach stages)
    if file_path in path_info:
        return path_info[file_path]["type"]
    
    # Strategy 2: Regex pattern match (foreach stages)
    for pat, stage_name, art_type in pattern_info:
        if pat.match(file_path):
            return art_type
    
    # Strategy 3: Check if file_path could match any declared template
    # by checking if it matches after removing the variable part
    for mp in declared_metrics_paths:
        if "${" in mp:
            # Build a simple prefix/suffix match
            parts = mp.split("${")
            prefix = parts[0]
            suffix = parts[-1].split("}")[-1] if "}" in parts[-1] else ""
            if file_path.startswith(prefix) and file_path.endswith(suffix):
                return "metric"
    
    for pp in declared_plots_paths:
        if "${" in pp:
            parts = pp.split("${")
            prefix = parts[0]
            suffix = parts[-1].split("}")[-1] if "}" in parts[-1] else ""
            if file_path.startswith(prefix) and file_path.endswith(suffix):
                return "plot"
    
    return None


@app.route('/api/projects/<path:repo>/branches', methods=['GET'])
def api_project_branches(repo):
    """List all distinct branches that have runs (completed or running) for this project."""
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    branches = []
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT DISTINCT branch, MAX(created_at) as last_run
                FROM jobs 
                WHERE repo = ? AND status IN ('completed', 'running', 'assigned')
                      AND commit_hash IS NOT NULL
                      AND branch IS NOT NULL
                GROUP BY branch
                ORDER BY last_run DESC
            ''', (repo,))
            branches = [{"name": row["branch"], "last_run": row["last_run"]} for row in cursor.fetchall()]
    except Exception:
        pass

    return jsonify(branches)


@app.route('/api/projects/<path:repo>/artifacts/latest', methods=['GET'])
def api_latest_artifacts(repo):
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    # Optional branch filter from query param (comma-separated)
    branch_filter = request.args.get('branches', '').strip()
    selected_branches = [b.strip() for b in branch_filter.split(',') if b.strip()] if branch_filter else None

    local_repo_path = find_local_repo(repo)
    if not local_repo_path:
        app.logger.warning(f"Local repo path not found for {repo}")
        return jsonify([])

    try:
        # 0. Ensure we have the latest state from remote
        subprocess.run(["git", "fetch", "--all", "--prune"], cwd=local_repo_path, capture_output=True, timeout=15)

        # 1. Determine which branches to scan
        branches_to_scan = []
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT DISTINCT branch
                    FROM jobs
                    WHERE repo = ? AND status IN ('completed', 'running', 'assigned', 'failed')
                          AND branch IS NOT NULL
                ''', (repo,))
                db_branches = [row['branch'] for row in cursor.fetchall()]
                branches_to_scan = db_branches
        except Exception:
            pass

        # Always include main/master as fallback
        if 'main' not in branches_to_scan:
            branches_to_scan.append('main')

        # Apply branch filter if specified
        if selected_branches:
            branches_to_scan = [b for b in branches_to_scan if b in selected_branches]
            if not branches_to_scan:
                branches_to_scan = selected_branches  # Use directly if no DB match

        # 2. For each branch, read dvc.lock from the latest Git HEAD (origin/<branch>)
        #    This captures intermediate watchdog commits, not just the DB-recorded commit.
        best_artifacts = {}  # path -> {artifact_data, date, branch}

        for branch in branches_to_scan:
            ref = f"origin/{branch}"

            # Verify the ref exists
            res_check = subprocess.run(
                ["git", "rev-parse", "--verify", ref],
                cwd=local_repo_path, capture_output=True, text=True, timeout=5
            )
            if res_check.returncode != 0:
                continue

            # Read dvc.yaml at this ref (for artifact type classification)
            dvc_yaml_data = {}
            try:
                res_yaml = subprocess.run(
                    ["git", "show", f"{ref}:dvc.yaml"],
                    cwd=local_repo_path, capture_output=True, text=True, timeout=5
                )
                if res_yaml.returncode == 0 and res_yaml.stdout.strip():
                    dvc_yaml_data = yaml.safe_load(res_yaml.stdout) or {}
            except Exception:
                pass

            # Read dvc.lock at this ref (source of truth for resolved artifact paths)
            dvc_lock_data = {}
            try:
                res_lock = subprocess.run(
                    ["git", "show", f"{ref}:dvc.lock"],
                    cwd=local_repo_path, capture_output=True, text=True, timeout=10
                )
                if res_lock.returncode == 0 and res_lock.stdout.strip():
                    dvc_lock_data = yaml.safe_load(res_lock.stdout) or {}
            except Exception:
                pass

            if not dvc_yaml_data and not dvc_lock_data:
                continue

            # Extract artifacts from this branch
            artifacts = extract_artifacts_from_lock(dvc_lock_data, dvc_yaml_data)
            if not artifacts:
                continue

            # Get the date of the latest commit that touched dvc.lock on this branch
            branch_date = None
            try:
                res_date = subprocess.run(
                    ["git", "log", ref, "-1", "--format=%aI", "--", "dvc.lock"],
                    cwd=local_repo_path, capture_output=True, text=True, timeout=5
                )
                if res_date.returncode == 0 and res_date.stdout.strip():
                    branch_date = res_date.stdout.strip()
            except Exception:
                pass

            # Get per-file last-modified dates for this branch
            file_dates = {}
            try:
                res_all = subprocess.run(
                    ["git", "log", ref, "--format=COMMIT_DATE:%aI", "--name-only", "--diff-filter=ACMR", "--"],
                    cwd=local_repo_path, capture_output=True, text=True, timeout=15
                )
                if res_all.returncode == 0:
                    current_date = None
                    for line in res_all.stdout.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("COMMIT_DATE:"):
                            current_date = line[len("COMMIT_DATE:"):]
                        elif current_date and line not in file_dates:
                            file_dates[line] = current_date
            except Exception:
                pass

            # For each artifact, keep the most recent version across branches
            for artifact in artifacts:
                path = artifact["path"]
                artifact_date = file_dates.get(path, branch_date)

                if path not in best_artifacts or (artifact_date and (
                    not best_artifacts[path]["created_at"] or
                    artifact_date > best_artifacts[path]["created_at"]
                )):
                    best_artifacts[path] = {
                        "path": path,
                        "is_dir": False,
                        "size": 0,
                        "isout": True,
                        "created_at": artifact_date,
                        "stage": artifact["stage"],
                        "artifact_type": artifact["artifact_type"],
                        "branch": branch
                    }

        files = list(best_artifacts.values())
        print(f"[Artifacts] Returning {len(files)} artifacts for {repo} (scanned {len(branches_to_scan)} branches)", flush=True)
        return jsonify(files)

    except Exception as e:
        app.logger.error(f"Error listing latest artifacts from local Git for {repo}: {e}")
        return jsonify([])

@app.route('/api/projects/<path:repo>/artifact/history', methods=['GET'])
def api_artifact_history(repo):
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    file_path = request.args.get('path', '')
    if not file_path:
        return jsonify({"error": "Missing 'path' parameter"}), 400

    local_repo_path = find_local_repo(repo)
    if not local_repo_path:
        return jsonify({"error": "Repository not cloned on headnode"}), 404

    # Fetch completed runs (all branches — cross-branch history)
    runs = []
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT job_id, branch, commit_hash, created_at, status 
                FROM jobs 
                WHERE repo = ? AND status = 'completed' AND commit_hash IS NOT NULL
                ORDER BY created_at DESC
            ''', (repo,))
            runs = [dict(row) for row in cursor.fetchall()]
    except Exception:
        pass

    if not runs:
        return jsonify([])

    # Map commit titles
    hashes = [run['commit_hash'] for run in runs if run.get('commit_hash')]
    title_map = {}
    if hashes:
        try:
            res = subprocess.run(
                ["git", "--no-pager", "show", "-s", "--format=%H|%s"] + hashes,
                cwd=local_repo_path,
                capture_output=True,
                text=True
            )
            for line in res.stdout.strip().split('\n'):
                if '|' in line:
                    h, t = line.split('|', 1)
                    title_map[h] = t
        except Exception:
            pass

    history = []
    
    # Resilient line-by-line YAML parser
    def parse_dvc_metadata(yaml_content, target_path):
        try:
            import yaml
            data = yaml.safe_load(yaml_content)
            if data:
                if 'outs' in data:
                    for out in data['outs']:
                        if out.get('path') == os.path.basename(target_path) or out.get('path') == target_path:
                            return {'md5': out.get('md5'), 'size': out.get('size')}
                if 'stages' in data:
                    for stage in data['stages'].values():
                        if 'outs' in stage:
                            for out in stage['outs']:
                                if out.get('path') == target_path or out.get('path') == os.path.basename(target_path):
                                    return {'md5': out.get('md5'), 'size': out.get('size')}
        except Exception:
            pass

        # Regex/line fallback
        blocks = []
        current_block = {}
        in_outs = False
        for line in yaml_content.splitlines():
            line_strip = line.strip()
            if not line_strip:
                continue
            if line_strip.startswith('outs:'):
                in_outs = True
                continue
            elif in_outs and len(line) - len(line.lstrip()) == 0:
                in_outs = False
            if in_outs or 'outs' in line_strip:
                if line_strip.startswith('-') or line_strip.startswith('path:'):
                    if current_block and 'path' in current_block:
                        blocks.append(current_block)
                        current_block = {}
                if ':' in line_strip:
                    parts = line_strip.split(':', 1)
                    key = parts[0].strip().replace('-', '').strip()
                    val = parts[1].strip().strip('"').strip("'")
                    if key in ['path', 'md5', 'size']:
                        current_block[key] = val
        if current_block and 'path' in current_block:
            blocks.append(current_block)
            
        for b in blocks:
            p = b.get('path', '')
            if p == target_path or p == os.path.basename(target_path) or target_path.endswith('/' + p):
                return {
                    'md5': b.get('md5'),
                    'size': int(b['size']) if b.get('size') and b['size'].isdigit() else None
                }
        return None

    for run in runs:
        commit = run['commit_hash']
        metadata = None
        
        # Try direct .dvc file
        dvc_file_path = file_path + ".dvc"
        try:
            res = subprocess.run(["git", "show", f"{commit}:{dvc_file_path}"], cwd=local_repo_path, capture_output=True, text=True)
            if res.returncode == 0:
                metadata = parse_dvc_metadata(res.stdout, file_path)
        except Exception:
            pass

        # Try dvc.lock
        if not metadata:
            try:
                res = subprocess.run(["git", "show", f"{commit}:dvc.lock"], cwd=local_repo_path, capture_output=True, text=True)
                if res.returncode == 0:
                    metadata = parse_dvc_metadata(res.stdout, file_path)
            except Exception:
                pass

        if metadata and metadata.get('md5'):
            history.append({
                'job_id': run['job_id'],
                'branch': run['branch'],
                'commit_hash': commit,
                'commit_title': title_map.get(commit, commit[:8]),
                'created_at': run['created_at'],
                'md5': metadata['md5'],
                'size': metadata['size']
            })

    return jsonify(history)

# Start background threads at import/WSGI startup (so it works under Gunicorn/uWSGI as well)
# We protect it to only run once in Werkzeug's reloader if debug is enabled.
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    try:
        init_db()
    except Exception as e:
        print(f"Error initializing DB at startup: {e}")
    
    threading.Thread(target=cleanup_inactive_viewers, daemon=True).start()
    threading.Thread(target=periodic_clean_ghosts, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
