import socket
# Force IPv4 to prevent infinite hangs on broken IPv6 networks (common on headless servers)
old_getaddrinfo = socket.getaddrinfo
def new_getaddrinfo(*args, **kwargs):
    responses = old_getaddrinfo(*args, **kwargs)
    return [response for response in responses if response[0] == socket.AF_INET]
socket.getaddrinfo = new_getaddrinfo
# Set a global timeout for all socket operations to prevent infinite hangs
socket.setdefaulttimeout(20.0)

from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, session, url_for, redirect, render_template
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
import tempfile
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
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

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
    protected_endpoints = ['register_worker', 'submit_job', 'update_job_status', 'worker_poll', 'notify_cleanup', 'maintenance_on', 'maintenance_off']
    if request.endpoint in protected_endpoints:
        if not check_token():
            return jsonify({"error": "Unauthorized"}), 401

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
    total_ram_gb = data.get('total_ram_gb')
    total_storage_gb = data.get('total_storage_gb')
    available_storage_gb = data.get('available_storage_gb')

    with get_db_conn() as conn:
        cursor = conn.cursor()
        # available_ram_gb is now a derived state, but we keep the column for backward compatibility
        # (it will be ignored by the dynamic calculation).
        cursor.execute('''
            INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, available_ram_gb, total_storage_gb, available_storage_gb, last_seen, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'online')
            ON CONFLICT(worker_id) DO UPDATE SET
                hostname = ?,
                service_url = ?,
                total_ram_gb = ?,
                total_storage_gb = ?,
                available_storage_gb = ?,
                last_seen = CURRENT_TIMESTAMP,
                status = 'online'
        ''', (worker_id, hostname, service_url, total_ram_gb, total_ram_gb, total_storage_gb, available_storage_gb, hostname, service_url, total_ram_gb, total_storage_gb, available_storage_gb))
        
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

def cancel_job_cleanly(job_id, exit_code=-15):
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
        return False

    status = job['status']
    if status not in ['pending', 'assigned', 'running']:
        return False

    # 1. Worker cancellation if active on worker
    if status in ['assigned', 'running'] and job['service_url']:
        try:
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

    return True

@app.route('/submit_job', methods=['POST'])
def submit_job():
    if MAINTENANCE_MODE:
        return jsonify({"error": "Service Unavailable: Maintenance Mode Active"}), 503
    data = request.json
    repo = data.get('repo')
    branch = data.get('branch')
    ram_required_gb = data.get('ram_required_gb', 0)
    max_runtime_hours = data.get('max_runtime_hours')
    exposed_port = data.get('exposed_port')
    custom_web_app = data.get('custom_web_app', False)
    gh_run_id = data.get('gh_run_id')
    gh_token = data.get('gh_token')
    env_vars = data.get('env_vars') # Dictionary of secrets
    job_id = str(uuid.uuid4())

    # Metadata extraction (Pre-flight check)
    required_hashes = []
    repo_url = f"https://github.com/{repo}.git"
    pat = os.environ.get("GITHUB_PAT")
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

    username = data.get('username')
    commit_hash = data.get('commit_hash')

    # 1. AUTO-CANCELLATION: Automatically cancel active jobs according to branch category
    jobs_to_cancel = []
    is_draft = branch.startswith("cluster-draft/") if branch else False
    if branch and repo:
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT job_id, branch FROM jobs
                    WHERE repo = ? AND status IN ('pending', 'assigned', 'running')
                ''', (repo,))
                active_jobs = cursor.fetchall()
                for aj in active_jobs:
                    aj_branch = aj['branch'] or ''
                    if is_draft:
                        if aj_branch == branch:
                            jobs_to_cancel.append(aj['job_id'])
                    else:
                        if not aj_branch.startswith("cluster-draft/"):
                            jobs_to_cancel.append(aj['job_id'])
        except Exception as e:
            app.logger.error(f"Error identifying active jobs to cancel: {e}")

    # Cancel identified jobs cleanly outside the active insertion transaction to prevent SQLite locks
    for j_id in jobs_to_cancel:
        try:
            app.logger.info(f"Auto-cancelling active job {j_id} on {repo} branch {branch} category match")
            cancel_job_cleanly(j_id, exit_code=-15)
        except Exception as e:
            app.logger.error(f"Failed to auto-cancel job {j_id}: {e}")

    # 2. Insert new job
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, commit_hash, ram_required_gb, max_runtime_hours, exposed_port, custom_web_app, gh_run_id, required_hashes, gh_token, env_vars, username, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        ''', (job_id, repo, branch, commit_hash, ram_required_gb, max_runtime_hours, exposed_port, 1 if custom_web_app else 0, gh_run_id, json.dumps(required_hashes), gh_token, json.dumps(env_vars) if env_vars else None, username))
        conn.commit()

    return jsonify({"job_id": job_id, "status": "pending", "required_hashes_count": len(required_hashes)})

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
                total_storage_gb, available_storage_gb, last_seen, status
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
            SELECT worker_id, hostname, service_url, total_ram_gb, status, last_seen
            FROM workers
        ''')
        workers_list = [dict(row) for row in cursor.fetchall()]
        
        for w in workers_list:
            cursor.execute('''
                SELECT job_id, repo, branch, username, ram_required_gb, max_runtime_hours, status, created_at, started_at
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
            SELECT job_id, repo, branch, username, ram_required_gb, status, created_at
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
        else:
            cursor.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (status, job_id))
        conn.commit()
    return jsonify({"status": "ok"})

@app.route('/clean_ghosts', methods=['POST'])
def clean_ghosts():
    """Cleans up ghost jobs (jobs that are pending in DB but their GH workflow is completed/cancelled)"""
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT job_id, repo, gh_run_id FROM jobs WHERE status = 'pending' AND gh_run_id IS NOT NULL")
        pending_jobs = cursor.fetchall()
        
        cleaned = 0
        gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_PAT")
        headers = {"Authorization": f"token {gh_token}", "Accept": "application/vnd.github.v3+json"} if gh_token else {}
        
        for job in pending_jobs:
            url = f"https://api.github.com/repos/{job['repo']}/actions/runs/{job['gh_run_id']}"
            try:
                resp = requests.get(url, headers=headers, timeout=5)
                if resp.status_code == 200:
                    run_data = resp.json()
                    status = run_data.get('status')
                    conclusion = run_data.get('conclusion')
                    if status == 'completed' or conclusion is not None:
                        app.logger.info(f"Ghost job detected: {job['job_id']}. Marking as failed.")
                        cursor.execute("UPDATE jobs SET status = 'failed', exit_code = -1, finished_at = CURRENT_TIMESTAMP WHERE job_id = ?", (job['job_id'],))
                        cleaned += 1
                elif resp.status_code == 404:
                    app.logger.info(f"Ghost job detected (404): {job['job_id']}. Marking as failed.")
                    cursor.execute("UPDATE jobs SET status = 'failed', exit_code = -1, finished_at = CURRENT_TIMESTAMP WHERE job_id = ?", (job['job_id'],))
                    cleaned += 1
            except Exception as e:
                app.logger.error(f"Error checking ghost job {job['job_id']}: {e}")
        
        if cleaned > 0:
            conn.commit()
            
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
    with get_db_conn() as conn:
        cursor = conn.cursor()
        # Try to find a worker that executed a job for this EXACT revision
        cursor.execute('''
            SELECT w.service_url
            FROM jobs j
            JOIN workers w ON j.worker_id = w.worker_id
            WHERE j.repo = ? AND j.commit_hash = ? AND w.status = 'online'
            ORDER BY j.finished_at DESC LIMIT 1
        ''', (repo_slug, rev))
        worker = cursor.fetchone()

        # Fallback: any online worker that has run jobs for this repo
        if not worker:
            cursor.execute('''
                SELECT w.service_url
                FROM jobs j
                JOIN workers w ON j.worker_id = w.worker_id
                WHERE j.repo = ? AND w.status = 'online'
                ORDER BY j.finished_at DESC LIMIT 1
            ''', (repo_slug,))
            worker = cursor.fetchone()

    if worker and worker['service_url']:
        inline_param = "&inline=true" if request.args.get("inline") == "true" else ""
        worker_url = f"{worker['service_url']}/api/worker/dvc/get?repo={repo_slug}&rev={rev}&path={file_path}{inline_param}"
        app.logger.info(f"[P2P] Proxying artifact {file_path}@{rev} to worker {worker['service_url']}")
        try:
            resp = proxy_request(worker_url)
            if resp.status_code < 500:
                return resp
            app.logger.warning(f"[P2P] Worker proxy returned {resp.status_code}, falling back to local extraction")
        except Exception as e:
            app.logger.warning(f"[P2P] Worker proxy failed: {e}, falling back to local extraction")

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
            SELECT job_id, branch, status, commit_hash, created_at, started_at, finished_at, exit_code, custom_web_app
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

def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port

def cleanup_inactive_viewers():
    """Background task to kill local dvc-viewer processes after inactivity."""
    while True:
        time.sleep(60)
        now = time.time()
        to_delete = []
        with local_viewers_lock:
            for repo_name, viewer in local_viewers.items():
                if now - viewer['last_access'] > (DVC_VIEWER_TIMEOUT_MIN * 60):
                    print(f"Terminating inactive dvc-viewer for {repo_name} (port {viewer['port']})")
                    try:
                        viewer['proc'].terminate()
                        viewer['proc'].wait(timeout=5)
                    except Exception as e:
                        print(f"Error terminating process: {e}")
                        try:
                            viewer['proc'].kill()
                        except:
                            pass
                    to_delete.append(repo_name)

            for repo_name in to_delete:
                del local_viewers[repo_name]

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

    async function checkApp() {{
        try {{
            const resp = await fetch(window.location.href, {{ method: 'HEAD', cache: 'no-store' }});
            if (resp.status === 200) {{
                statusElement.textContent = "✅ App is Ready! Redirecting...";
                statusElement.style.color = "#4ade80";
                setTimeout(() => window.location.reload(), 500);
                return;
            }}
        }} catch (e) {{ }}
        setTimeout(checkApp, 2000);
    }}

    setInterval(pollLogs, 2000);
    pollLogs();
    checkApp();
</script>
</body></html>""", 502
        return result

    # --- Case 2: Historical (Local) ---
    repo_path = find_local_repo(repo_full_name)
    if not repo_path:
        return f"Project {repo_full_name} not found locally and not active.", 404

    with local_viewers_lock:
        if repo_full_name in local_viewers:
            viewer = local_viewers[repo_full_name]
            # Check if process is still alive
            if viewer['proc'].poll() is None:
                viewer['last_access'] = time.time()
                target_url = f"http://localhost:{viewer['port']}/{path}"
                base_href = f"/view/{owner}/{repo}/" if path == '' else None
                return proxy_request(target_url, base_href=base_href)
            else:
                del local_viewers[repo_full_name]

        # Start a new dvc-viewer process
        port = get_free_port()
        try:
            # Start global dvc-viewer and inject read-only mode (CLUSTER_CI_MODE=executor)
            viewer_env = os.environ.copy()
            viewer_env["CLUSTER_CI_MODE"] = "executor"
            viewer_env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + viewer_env.get("PATH", "")
            cmd = ["dvc-viewer", "serve", "--port", str(port)]
            proc = subprocess.Popen(cmd, cwd=repo_path, env=viewer_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Wait a bit for the server to start
            time.sleep(2)

            local_viewers[repo_full_name] = {
                'proc': proc,
                'port': port,
                'last_access': time.time()
            }
            target_url = f"http://localhost:{port}/{path}"
            base_href = f"/view/{owner}/{repo}/" if path == '' else None
            return proxy_request(target_url, base_href=base_href)
        except Exception as e:
            return f"Failed to start dvc-viewer: {str(e)}", 500

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

@app.route('/api/projects/<path:repo>/artifacts/latest', methods=['GET'])
def api_latest_artifacts(repo):
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT commit_hash, branch, job_id, created_at 
            FROM jobs 
            WHERE repo = ? AND status = 'completed' AND commit_hash IS NOT NULL
            ORDER BY created_at DESC LIMIT 1
        ''', (repo,))
        last_run = cursor.fetchone()

    if not last_run:
        return jsonify([])

    commit_hash = last_run['commit_hash']
    local_repo_path = find_local_repo(repo)
    pat = os.environ.get("GITHUB_PAT")
    repo_url = f"https://x-access-token:{pat}@github.com/{repo}.git" if pat else f"https://github.com/{repo}.git"
    source = local_repo_path if local_repo_path else repo_url

    cmd = [DVC_CMD, "list", source, "--rev", commit_hash, "--dvc-only", "--recursive", "--json"]
    try:
        env = os.environ.copy()
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode == 0:
            return Response(result.stdout, mimetype='application/json')
        else:
            # Try fetching if unknown Git revision
            if local_repo_path and "unknown Git revision" in result.stderr:
                subprocess.run(["git", "fetch", "--all", "--prune"], cwd=local_repo_path, capture_output=True, timeout=30)
                result = subprocess.run(cmd, capture_output=True, text=True, env=env)
                if result.returncode == 0:
                    return Response(result.stdout, mimetype='application/json')
            return jsonify({"error": "Failed to list DVC files", "details": result.stderr}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

    # Fetch completed runs
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT job_id, branch, commit_hash, created_at, status 
            FROM jobs 
            WHERE repo = ? AND status = 'completed' AND commit_hash IS NOT NULL
            ORDER BY created_at DESC
        ''', (repo,))
        runs = [dict(row) for row in cursor.fetchall()]

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

if __name__ == '__main__':
    init_db()
    # Start cleanup thread
    threading.Thread(target=cleanup_inactive_viewers, daemon=True).start()
    threading.Thread(target=periodic_clean_ghosts, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
