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
from src.scheduler.utils.git_dvc import find_local_repo, resolve_revision, get_dvc_artifacts, filter_artifact_files, REPOS_DIR, GitError

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
    if service_url:
        service_url = service_url.replace("1300.223.169.200", "130.223.169.200")
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
        # We must mark any 'running' or 'assigned' jobs for this worker as 'failed'.
        is_startup = data.get('is_startup', False)
        if is_startup:
            cursor.execute('''
                UPDATE jobs
                SET status = 'failed', exit_code = COALESCE(exit_code, -98)
                WHERE worker_id = ? AND status IN ('running', 'assigned')
            ''', (worker_id,))
        
        conn.commit()

    return jsonify({"status": "ok"})

@app.route('/submit_job', methods=['POST'])
def submit_job():
    if MAINTENANCE_MODE:
        return jsonify({"error": "Service Unavailable: Maintenance Mode Active"}), 503
    data = request.json
    repo = data.get('repo')
    branch = data.get('branch')
    ram_required_gb = data.get('ram_required_gb', 0)
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

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, ram_required_gb, required_hashes, gh_token, env_vars, username, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        ''', (job_id, repo, branch, ram_required_gb, json.dumps(required_hashes), gh_token, json.dumps(env_vars) if env_vars else None, username))
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
            cursor.execute('''
                SELECT repo, status, job_id 
                FROM jobs j1 
                WHERE created_at = (SELECT MAX(created_at) FROM jobs j2 WHERE j1.repo = j2.repo)
            ''')
            db_projects = {}
            for row in cursor.fetchall():
                db_projects[row['repo']] = {'status': row['status'], 'job_id': row['job_id']}

        # Only return projects that are in the database AND the user has access to (case-insensitive)
        projects = []
        for p, info in db_projects.items():
            if p.lower() in allowed_repos:
                projects.append({
                    "name": p,
                    "status": info['status'],
                    "job_id": info['job_id']
                })
        
        # Sort projects alphabetically by name
        projects.sort(key=lambda x: x['name'].lower())
        return jsonify(projects)
    except Exception as e:
        app.logger.error(f"Error fetching repos in API: {e}")
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

@app.route('/api/projects/<path:repo>/runs', methods=['GET'])
def api_list_runs(repo):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT job_id, branch, status, commit_hash, created_at, started_at, finished_at, exit_code
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
        return jsonify({"error": "Job not found"}), 404

    # 1. Cancel on Worker (Safety check: prevent zombie containers)
    if job['status'] in ['running', 'assigned'] and job['service_url']:
        try:
            resp = requests.post(f"{job['service_url']}/cancel/{job_id}", timeout=10)
            # 200: Success, 404: Job not on worker (safe to proceed)
            if resp.status_code not in [200, 404]:
                return jsonify({"error": f"Worker failed to cancel job (HTTP {resp.status_code}): {resp.text}"}), 502
        except Exception as e:
            app.logger.error(f"Failed to send cancel to worker: {e}")
            return jsonify({"error": f"Could not reach worker to verify job cancellation: {str(e)}"}), 504

    # 2. Cancel on GitHub Actions (if run_id exists) - Best effort
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
            app.logger.error(f"Failed to cancel GH Action: {e}")

    # 3. Update Status in DB (Only reached if worker confirmed or unreachable with error returned above)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE jobs
            SET status = 'failed', exit_code = -1, finished_at = CURRENT_TIMESTAMP
            WHERE job_id = ?
        ''', (job_id,))
        conn.commit()

    return jsonify({"status": "ok", "message": "Job stopped and verified"})

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

    if not local_repo_path:
        app.logger.warning(f"Local repo path not found for {repo}")
        return jsonify([])

    try:
        # Run git ls-tree to list all files at that commit
        cmd = ["git", "ls-tree", "-r", "--name-only", commit_hash]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=local_repo_path, timeout=5)

        if result.returncode != 0:
            # If the commit is not locally known, try resolving it (includes fetch)
            try:
                commit_hash = resolve_revision(local_repo_path, commit_hash)
                result = subprocess.run(cmd, capture_output=True, text=True, cwd=local_repo_path, timeout=5)
            except GitError as ge:
                return jsonify({"error": f"Repository sync failed: {str(ge)}"}), 503

        if result.returncode == 0:
            all_git_files = [l for l in result.stdout.strip().split("\n") if l]
            dvc_artifacts = get_dvc_artifacts(local_repo_path, commit_hash)
            files = filter_artifact_files(all_git_files, dvc_artifacts)
            return jsonify(files)
        else:
            app.logger.error(f"Git ls-tree failed for {repo}: {result.stderr}")
            return jsonify({"error": f"Failed to list repository files: {result.stderr}"}), 500

    except Exception as e:
        app.logger.error(f"Error listing latest artifacts from local Git for {repo}: {e}")
        return jsonify({"error": f"Internal error while listing artifacts: {str(e)}"}), 500

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
            # Viewer is unreachable — show a diagnostic page instead of raw error
            diag_msg = "DVC Viewer container may have crashed or failed to start."
            try:
                # Try to get viewer logs from worker
                log_resp = requests.get(f"{worker_base_url}/viewer_logs", timeout=3)
                if log_resp.status_code == 200:
                    viewer_logs = log_resp.json().get('logs', '')
                    if viewer_logs:
                        diag_msg = f"Viewer container logs:\n{viewer_logs[-2000:]}"
            except Exception:
                pass

            return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>DVC Viewer — Unavailable</title>
<meta http-equiv="refresh" content="10">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #1a1a2e; color: #e0e0e0;
       display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
.card {{ background: #16213e; border-radius: 12px; padding: 40px; max-width: 700px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); }}
h1 {{ color: #e94560; margin-top: 0; }} pre {{ background: #0f3460; padding: 16px; border-radius: 8px; overflow-x: auto; font-size: 0.85em; max-height: 300px; overflow-y: auto; }}
.hint {{ color: #a0a0a0; font-size: 0.9em; margin-top: 20px; }}
</style></head>
<body><div class="card">
<h1>⚠️ DVC Viewer Unreachable</h1>
<p>The live viewer at <code>{target_host}:{viewer_port}</code> is not responding.</p>
<pre>{diag_msg}</pre>
<p class="hint">🔄 This page will auto-refresh every 10 seconds.<br>
💡 Check <code>dvc-viewer.log</code> on the worker for details.</p>
</div></body></html>""", 502
        return result

    # --- Case 2: Historical (Remote Worker) ---
    rev = request.args.get('rev')
    
    # Try to find default commit hash from DB if not provided
    if not rev:
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT commit_hash FROM jobs
                    WHERE repo = ? AND status = 'completed' AND commit_hash IS NOT NULL
                    ORDER BY finished_at DESC LIMIT 1
                ''', (repo_full_name,))
                row = cursor.fetchone()
                if row:
                    rev = row['commit_hash']
                else:
                    rev = 'main'
        except Exception as e:
            app.logger.error(f"Error fetching default commit hash for historical view: {e}")
            rev = 'main'

    # Resolve rev to an absolute commit SHA to prevent "zombie" stages when a branch moves
    local_repo_path = find_local_repo(repo_full_name)
    try:
        rev = resolve_revision(local_repo_path, rev)
    except GitError as e:
        app.logger.warning(f"Revision resolution failed for {repo_full_name} ({rev}): {e}")

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
            stream=True
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

if __name__ == '__main__':
    init_db()
    # Start cleanup thread
    threading.Thread(target=cleanup_inactive_viewers, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
