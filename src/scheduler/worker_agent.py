import time
import requests
import os
import sys
import socket
import psutil
import subprocess
import logging
import uuid
import threading
import json
import tempfile
import shutil
import signal
import datetime
from flask import Flask, jsonify, send_from_directory, send_file, request, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADNODE_URL = os.environ.get("HEADNODE_URL", "http://localhost:5000")
CLUSTER_TOKEN = os.environ.get("CLUSTER_TOKEN")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOGS_DIR = os.path.join(BASE_DIR, "job_logs")
os.makedirs(LOGS_DIR, exist_ok=True)

def get_headers():
    headers = {}
    if CLUSTER_TOKEN:
        headers["Authorization"] = f"Bearer {CLUSTER_TOKEN}"
    return headers

def purge_orphan_runners_and_containers(job_id=None):
    """Performs JIT (Just-In-Time) purge of orphan docker containers and runner processes.
    
    If job_id is provided, avoids destroying containers associated with this job.
    Otherwise, destroys all cluster-job-* and cluster-viewer-* containers.
    """
    logger.info(f"🧹 Performing JIT (Just-In-Time) purge of orphan runners and containers (Job ID context: {job_id})")
    
    # Unleash proactive Ollama Host VRAM Purge to instantly reclaim physical resources before starting/cleaning up
    purge_ollama_vram_on_host()
    
    # 1. Docker JIT Container Purge
    safe_job_id = job_id.replace('/', '-') if job_id else None
    expected_containers = {f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"} if safe_job_id else set()
    
    try:
        res = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=cluster-job-", "--filter", "name=cluster-viewer-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15
        )
        if res.returncode == 0:
            containers = [c.strip() for c in res.stdout.split("\n") if c.strip()]
            for container in containers:
                if container not in expected_containers:
                    logger.warning(f"🔥 JIT Purge: Destroying orphan/zombie container {container}...")
                    subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10)
        else:
            logger.error(f"JIT Purge: Failed to list docker containers: {res.stderr}")
    except Exception as e:
        logger.error(f"JIT Purge: Error during docker container purge: {e}")
        
    # 2. Host Orphan Process Purge (including dvc-viewer and python runners)
    my_pid = os.getpid()
    logger.info("Scanning for orphan runner/viewer processes on host...")
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            pid = proc.info.get('pid')
            if pid == my_pid:
                continue
            
            cmdline = proc.info.get('cmdline') or []
            cmdline_str = " ".join(cmdline).lower()
            name = (proc.info.get('name') or "").lower()
            
            is_orphan_runner = False
            if "cluster-ci-run" in cmdline_str or "gc_orchestrator" in cmdline_str:
                is_orphan_runner = True
            if "dvc-viewer" in cmdline_str or name == "dvc-viewer":
                is_orphan_runner = True
            
            if is_orphan_runner:
                logger.warning(f"🔥 JIT Purge: Killing host orphan process (PID: {pid}, name: {name}, cmd: {cmdline})")
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

def purge_ollama_vram_on_host():
    """Contact local Ollama service on host (if active) to unload all active models and free GPU VRAM instantly."""
    logger.info("📡 Requesting local Ollama service to purge all models from GPU VRAM...")
    ollama_url = "http://127.0.0.1:11434"
    try:
        # 1. Get list of active/loaded models in memory
        resp = requests.get(f"{ollama_url}/api/ps", timeout=3)
        if resp.status_code == 200:
            models_data = resp.json()
            models = models_data.get("models", [])
            if not models:
                logger.info("Ollama memory is already clean (0 models loaded).")
                return
                
            for m in models:
                name = m.get("name") or m.get("model")
                if name:
                    logger.warning(f"🔥 Forcing unload of Ollama model '{name}' from GPU VRAM...")
                    # Sending keep_alive: 0 or keep_alive: "0s" forces immediate unload
                    requests.post(
                        f"{ollama_url}/api/generate",
                        json={"model": name, "keep_alive": 0},
                        timeout=3
                    )
            logger.info("✅ Successfully requested Ollama to unload all models.")
        else:
            logger.info(f"Ollama API /api/ps returned status {resp.status_code}. Skipping Ollama VRAM purge.")
    except Exception as e:
        logger.info(f"Ollama service not running or unreachable on host: {e}. Skipping host Ollama VRAM purge.")

def kill_dvc_viewer_processes():
    # Deprecated wrapper: delegate to our robust purge function
    purge_orphan_runners_and_containers()

# Generate or load a persistent worker ID
WORKER_ID_FILE = "worker_id.txt"
if os.path.exists(WORKER_ID_FILE):
    with open(WORKER_ID_FILE, 'r') as f:
        WORKER_ID = f.read().strip()
else:
    WORKER_ID = str(uuid.uuid4())
    with open(WORKER_ID_FILE, 'w') as f:
        f.write(WORKER_ID)

HOSTNAME = socket.gethostname()
AGENT_PORT = int(os.environ.get("AGENT_PORT", 6000))
SERVICE_URL = os.environ.get("SERVICE_URL", f"http://{HOSTNAME}:{AGENT_PORT}")

# Global state for current job tracking
current_job_id = None
current_process = None
job_lock = threading.Lock()
startup_heartbeat_event = threading.Event()
pending_update_restart = False  # Set when update_self defers a restart during an active job

def get_ram_info():
    mem = psutil.virtual_memory()
    total_gb = mem.total / (1024**3)
    available_gb = mem.available / (1024**3)
    return total_gb, available_gb

def get_storage_info():
    try:
        # Use the repositories directory if it exists, otherwise the root of the project
        target_path = REPOS_DIR if os.path.exists(REPOS_DIR) else BASE_DIR
        usage = shutil.disk_usage(target_path)
        total_gb = usage.total / (1024**3)
        available_gb = usage.free / (1024**3)
        return total_gb, available_gb
    except Exception as e:
        logger.error(f"Error getting storage info: {e}")
        return 0.0, 0.0

def heartbeat_loop():
    is_startup = True
    while True:
        total_ram_gb, available_ram_gb = get_ram_info()
        total_storage_gb, available_storage_gb = get_storage_info()
        try:
            resp = requests.post(f"{HEADNODE_URL}/register_worker", json={
                "worker_id": WORKER_ID,
                "hostname": HOSTNAME,
                "service_url": SERVICE_URL,
                "total_ram_gb": total_ram_gb,
                "available_ram_gb": available_ram_gb,
                "total_storage_gb": total_storage_gb,
                "available_storage_gb": available_storage_gb,
                "is_startup": is_startup
            }, headers=get_headers(), timeout=10)
            resp.raise_for_status()
            is_startup = False
            startup_heartbeat_event.set()
        except Exception as e:
            logger.error(f"Failed to send heartbeat: {e}")
        time.sleep(10)

def poll_for_job():
    try:
        resp = requests.get(f"{HEADNODE_URL}/worker_poll/{WORKER_ID}", headers=get_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("job_id"):
            return data
    except Exception as e:
        logger.error(f"Failed to poll: {e}")
    return None

def update_job_status(job_id, status, exit_code=None, commit_hash=None, viewer_port=None):
    payload = {"job_id": job_id, "status": status}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if commit_hash is not None:
        payload["commit_hash"] = commit_hash
    if viewer_port is not None:
        payload["viewer_port"] = viewer_port

    delay = 5
    max_attempts = 7  # 1 initial attempt + up to 6 retries
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(f"{HEADNODE_URL}/update_job_status", json=payload, headers=get_headers(), timeout=10)
            resp.raise_for_status()
            if attempt > 1:
                logger.info(f"Successfully updated job status to '{status}' on attempt {attempt}")
            return
        except Exception as e:
            if attempt < max_attempts:
                logger.warning(
                    f"Attempt {attempt}/{max_attempts} failed to update job status to '{status}' for job {job_id}: {e}. "
                    f"Retrying in {delay} seconds..."
                )
                time.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    f"❌ CRITICAL: All {max_attempts} attempts failed to update job status to '{status}' for job {job_id}: {e}"
                )
                raise

def execute_job(job):
    global current_job_id, current_process
    job_id = job['job_id']
    repo = job['repo']
    branch = job['branch']
    ram_limit_gb = job['ram_required_gb']
    max_runtime_hours = job.get('max_runtime_hours')
    p2p_url = job.get('p2p_url')
    gh_token = job.get('gh_token')
    env_vars = job.get('env_vars')

    logger.info(f"Executing job {job_id} for {repo}@{branch} with {ram_limit_gb}GB limit")
    purge_orphan_runners_and_containers(job_id)
    update_job_status(job_id, 'running')

    with job_lock:
        current_job_id = job_id

    # We call the cluster-ci-run command which is supposed to be in /usr/local/bin/cluster-ci-run
    # or provided via CLUSTER_CI_RUN_PATH environment variable
    executable = os.environ.get("CLUSTER_CI_RUN_PATH", "/usr/local/bin/cluster-ci-run")
    cmd = [executable, repo, branch]

    env = os.environ.copy()
    env["CLUSTER_CI_MODE"] = "executor"
    env["JOB_ID"] = job_id
    env["LOGS_DIR"] = LOGS_DIR
    commit_hash = job.get('commit_hash')
    if commit_hash:
        logger.info(f"Injecting CALLER_COMMIT_SHA for job {job_id}: {commit_hash}")
        env["CALLER_COMMIT_SHA"] = commit_hash
    if p2p_url:
        logger.info(f"Injecting P2P URL for job {job_id}: {p2p_url}")
        env["DVC_REMOTE_P2P_URL"] = p2p_url
    if gh_token:
        logger.info(f"Injecting GH_TOKEN for job {job_id}")
        env["GH_TOKEN"] = gh_token

    secrets_file = None
    if env_vars:
        try:
            parsed_vars = json.loads(env_vars) if isinstance(env_vars, str) else env_vars
            if parsed_vars:
                # Create a secure temp file for job secrets
                fd, secrets_file = tempfile.mkstemp(prefix=f"job_secrets_{job_id}_", suffix=".env")
                with os.fdopen(fd, 'w') as f:
                    for k, v in parsed_vars.items():
                        f.write(f"{k}={v}\n")
                logger.info(f"Injecting {len(parsed_vars)} custom environment variables via {secrets_file}")
                env["CLUSTER_CI_SECRETS_FILE"] = secrets_file
        except Exception as e:
            logger.error(f"Failed to write job secrets: {e}")

    log_path = os.path.join(LOGS_DIR, f"{job_id}.log")
    log_file = open(log_path, 'w')

    try:
        # Delete stale port file from previous runs
        port_file = os.path.join(REPOS_DIR, repo, ".cluster-ci-viewer-port")
        if os.path.exists(port_file):
            try:
                os.remove(port_file)
            except Exception as e:
                logger.warning(f"Could not remove stale port file {port_file}: {e}")

        process = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        with job_lock:
            current_process = process

        # Launch an unbuffered line-by-line real-time log streamer thread
        import threading
        def log_streamer():
            try:
                for line in process.stdout:
                    log_file.write(line)
                    log_file.flush()
                    try:
                        os.fsync(log_file.fileno())
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Error in log streamer: {e}")

        streamer_thread = threading.Thread(target=log_streamer, daemon=True)
        streamer_thread.start()

        port_reported = False
        start_time = time.time()
        timeout_seconds = (max_runtime_hours * 3600) if max_runtime_hours else (24 * 3600)

        # Status monitoring loop
        while process.poll() is None:
            time.sleep(5)
            # 1. Watchdog: Check for timeout
            elapsed = time.time() - start_time
            if elapsed > timeout_seconds:
                logger.error(f"❌ [WATCHDOG] Job {job_id} exceeded its {max_runtime_hours}h limit. Triggering forced destruction.")
                error_msg = f"\n❌ [CLUSTER WATCHDOG] Job exceeded maximum runtime of {max_runtime_hours} hours. Terminating.\n"
                log_file.write(error_msg)
                log_file.flush()

                # Inconditional destruction
                safe_job_id = job_id.replace('/', '-')
                subprocess.run(["docker", "rm", "-f", f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"], capture_output=True)
                process.terminate()
                break

            # Try to report dynamic viewer port if not already done
            if not port_reported:
                port_file = os.path.join(REPOS_DIR, repo, ".cluster-ci-viewer-port")
                if os.path.exists(port_file):
                    try:
                        with open(port_file, 'r') as f:
                            viewer_port = int(f.read().strip())
                        logger.info(f"Reporting dynamic viewer port {viewer_port} for job {job_id}")
                        update_job_status(job_id, 'running', viewer_port=viewer_port)
                        port_reported = True
                    except Exception as e:
                        logger.error(f"Failed to read/report viewer port: {e}")

            time.sleep(2)

        exit_code = process.wait()
        streamer_thread.join(timeout=10)

        # Try to extract the commit hash from the job's directory
        commit_hash = None
        commit_file = os.path.join(REPOS_DIR, repo, ".cluster-ci-commit")
        if os.path.exists(commit_file):
            try:
                with open(commit_file, 'r') as f:
                    commit_hash = f.read().strip()
            except Exception as e:
                logger.error(f"Failed to read commit hash file: {e}")

        if exit_code == 137:
            error_msg = f"❌ [CLUSTER INTERRUPTED] Execution interrupted (Exit code 137). This usually means an OOM (Out of Memory) or a Zombie Job Cleanup.\n"
            sys.stderr.write(error_msg)
            sys.stderr.flush()
            log_file.write(error_msg)
            try:
                res = subprocess.run("sudo dmesg -T | grep -i -E 'oom|kill' | tail -n 30", shell=True, capture_output=True, text=True)
                if res.stdout.strip():
                    log_file.write("\n--- SYSTEM DMESG (Kernel OOM Logs) ---\n")
                    log_file.write(res.stdout)
                    log_file.write("--------------------------------------\n")
            except:
                pass
            log_file.flush()
            update_job_status(job_id, 'failed', 137, commit_hash=commit_hash)
        elif exit_code == 0:
            update_job_status(job_id, 'completed', exit_code, commit_hash=commit_hash)
        elif exit_code < 0:
            # Likely killed by a signal (cancellation)
            logger.info(f"Job {job_id} was killed (exit code {exit_code})")
            update_job_status(job_id, 'failed', exit_code, commit_hash=commit_hash)
        else:
            update_job_status(job_id, 'failed', exit_code, commit_hash=commit_hash)

    except Exception as e:
        logger.error(f"Execution failed: {e}")
        update_job_status(job_id, 'failed', -1)
    finally:
        kill_dvc_viewer_processes()
        if 'log_file' in locals() and not log_file.closed:
            log_file.close()
        should_restart = False
        with job_lock:
            current_job_id = None
            current_process = None
            if pending_update_restart:
                should_restart = True
        if secrets_file and os.path.exists(secrets_file):
            try:
                os.remove(secrets_file)
                logger.info(f"Cleaned up secrets file: {secrets_file}")
            except Exception as e:
                logger.error(f"Failed to cleanup secrets file: {e}")
        if should_restart:
            logger.info("Job finished — executing deferred update restart that was postponed during job execution.")
            _trigger_deferred_restart()

def drain_pending_syncs():
    logger.info("Starting drain of pending synchronizations...")

    # Path to registry.json
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    registry_path = os.path.join(base_dir, "repositories", "registry.json")

    logger.info(f"Looking for registry at: {registry_path}")
    if not os.path.exists(registry_path):
        logger.info("No registry.json found, nothing to drain.")
        return

    try:
        with open(registry_path, 'r') as f:
            registry = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load registry: {e}")
        return

    for project_name, data in registry.items():
        if data.get("sync_status") == "pending":
            logger.info(f"Project {project_name} has pending sync. Checking headnode space...")
            try:
                resp = requests.get(f"{HEADNODE_URL}/check_space", timeout=5, headers=get_headers())
                resp.raise_for_status()
                space_info = resp.json()

                if space_info.get("sufficient"):
                    logger.info(f"Headnode space sufficient. Pushing {project_name}...")
                    project_dir = os.path.join(base_dir, "repositories", project_name)
                    if os.path.exists(project_dir):
                        # Check if a default DVC remote is configured
                        has_remote = False
                        dvc_config_path = os.path.join(project_dir, ".dvc", "config")
                        dvc_config_local_path = os.path.join(project_dir, ".dvc", "config.local")
                        
                        for config_path in [dvc_config_path, dvc_config_local_path]:
                            if os.path.exists(config_path):
                                with open(config_path, "r") as f:
                                    content = f.read()
                                    import re
                                    if re.search(r"^\s*remote\s*=", content, re.MULTILINE):
                                        has_remote = True
                                        break

                        if not has_remote:
                            logger.info(f"No default DVC remote configured for {project_name}. Skipping push.")
                            subprocess.run(["python3", os.path.join(base_dir, "src/runner/gc_orchestrator.py"), "mark-sync-done", project_name])
                        else:
                            # Execute dvc push via uv
                            res = subprocess.run(["uv", "run", "dvc", "push"], cwd=project_dir)
                            if res.returncode == 0:
                                # Mark as done
                                subprocess.run(["python3", os.path.join(base_dir, "src/runner/gc_orchestrator.py"), "mark-sync-done", project_name])
                                logger.info(f"Successfully pushed and marked {project_name} as done.")
                            else:
                                logger.error(f"dvc push failed for {project_name}")
                    else:
                        logger.warning(f"Project directory {project_dir} not found for {project_name}")
                else:
                    logger.info(f"Headnode still full ({space_info.get('free_gb'):.2f} GB free). Stopping drain.")
                    break
            except Exception as e:
                logger.error(f"Error during drain for {project_name}: {e}")

# Webhook server
app = Flask(__name__)

REPOS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "repositories")

@app.route('/cancel/<job_id>', methods=['POST'])
def cancel_job(job_id):
    global current_job_id, current_process
    logger.info(f"Received cancellation request for job {job_id}")

    safe_job_id = job_id.replace('/', '-')
    # Strict Docker Purge (Isolation & Clean Kill)
    logger.info(f"Forcing destruction of containers for job {job_id}")
    res = subprocess.run(["docker", "rm", "-f", f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"],
                         capture_output=True, text=True)
    
    # Proactively purge host Ollama VRAM to instantly free Blackwell GPU physical memory
    purge_ollama_vram_on_host()
    
    kill_dvc_viewer_processes()

    with job_lock:
        if current_job_id == job_id and current_process:
            logger.info(f"Cancelling job {job_id}: killing Docker containers and process tree")
            try:
                # 1. Force-remove Docker containers (primary kill mechanism)
                # The containers follow the naming convention from run_research_pipeline.sh
                safe_job_id = job_id.replace('/', '-')
                for prefix in ["cluster-job-", "cluster-viewer-"]:
                    container_name = f"{prefix}{safe_job_id}"
                    logger.info(f"Force-removing container: {container_name}")
                    subprocess.run(
                        ["docker", "rm", "-f", container_name],
                        capture_output=True, timeout=10
                    )

                # 2. Kill process tree on host (belt-and-suspenders)
                try:
                    parent = psutil.Process(current_process.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass

                # 3. Proactively update job status on headnode
                # execute_job() will also update on process exit, but we do it
                # here immediately so the DB is consistent even if cleanup is slow.
                update_job_status(job_id, 'failed', exit_code=-15)

                return jsonify({"status": "cancelled", "message": "Docker containers removed and process tree killed"}), 200
            except Exception as e:
                logger.error(f"Error while cancelling job: {e}")
                # Still try to mark the job as failed even if cleanup had errors
                update_job_status(job_id, 'failed', exit_code=-15)
                return jsonify({"status": "error", "message": str(e)}), 500
        else:
            if res.returncode == 0:
                return jsonify({"status": "not_found", "message": "Job not active on this worker and no containers found"}), 404
            return jsonify({"status": "not_found", "message": "Job not active on this worker"}), 404

@app.route('/job_logs/<job_id>', methods=['GET'])
def get_job_logs(job_id):
    offset = int(request.args.get('offset', 0))
    log_path = os.path.join(LOGS_DIR, f"{job_id}.log")
    
    if not os.path.exists(log_path):
        return jsonify({"logs": "", "offset": offset})
        
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(offset)
            new_logs = f.read()
            new_offset = f.tell()
        return jsonify({"logs": new_logs, "offset": new_offset})
    except Exception as e:
        logger.error(f"Error reading logs for {job_id}: {e}")
        return jsonify({"logs": "", "offset": offset}), 500

@app.route('/viewer_logs', methods=['GET'])
def get_viewer_logs():
    """Return the last 2000 chars of the dvc-viewer log file for diagnostics."""
    log_path = os.path.join(BASE_DIR, "dvc-viewer.log")
    if not os.path.exists(log_path):
        return jsonify({"logs": "No dvc-viewer.log found on this worker."})
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return jsonify({"logs": content[-2000:] if len(content) > 2000 else content})
    except Exception as e:
        return jsonify({"logs": f"Error reading dvc-viewer.log: {e}"}), 500

@app.route('/crash_report', methods=['GET'])
def get_crash_report():
    """Return recent kernel OOM/kill logs to help diagnose -98 errors."""
    try:
        res = subprocess.run(
            "sudo dmesg -T | grep -i -E 'oom|kill' | tail -n 50",
            shell=True, capture_output=True, text=True
        )
        syslog = subprocess.run(
            "sudo journalctl -u cluster-worker -n 50 --no-pager",
            shell=True, capture_output=True, text=True
        )
        return jsonify({
            "dmesg": res.stdout,
            "syslog": syslog.stdout
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/fetch_artifact/<path:file_path>', methods=['GET'])
def fetch_artifact(file_path):
    """
    Serves a file from the repositories directory.
    send_from_directory provides protection against directory traversal.
    """
    logger.info(f"Worker received request for artifact: {file_path}")
    return send_from_directory(REPOS_DIR, file_path)

@app.route('/check_cache', methods=['POST'])
def check_cache():
    """
    Checks if the worker has the specified DVC cache files.
    Input JSON: {"repo": "owner/repo", "hashes": ["hash1", "hash2", ...]}
    Returns: JSON list of hashes present on this worker.
    """
    data = request.get_json()
    if not data or 'repo' not in data or 'hashes' not in data:
        return jsonify({"error": "Missing repo or hashes"}), 400

    repo = data['repo']
    hashes = data['hashes']
    found_hashes = []

    for h in hashes:
        if len(h) < 2:
            continue
        # DVC CAS nomenclature: .dvc/cache/files/md5/<2_chars>/<rest>
        cache_path = os.path.join(REPOS_DIR, repo, ".dvc", "cache", "files", "md5", h[:2], h[2:])
        if os.path.exists(cache_path):
            found_hashes.append(h)

    return jsonify(found_hashes)

def get_executable(name):
    """Finds an executable in system PATH, local bin, or current venv."""
    cmd = shutil.which(name)
    if cmd: return cmd
    local_path = os.path.expanduser(f"~/.local/bin/{name}")
    if os.path.exists(local_path): return local_path
    venv_path = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(venv_path): return venv_path
    return name

DVC_CMD = get_executable("dvc")

@app.route('/api/worker/dvc/list', methods=['GET'])
def worker_dvc_list():
    repo = request.args.get('repo')
    rev = request.args.get('rev')
    if not repo: return jsonify({"error": "Missing repo"}), 400

    repo_path = os.path.join(REPOS_DIR, repo)
    if not os.path.exists(repo_path):
        return jsonify({"error": "Repository not found on this worker"}), 404

    cmd = [DVC_CMD, "list", ".", "--dvc-only", "--json"]
    if rev: cmd += ["--rev", rev]

    try:
        res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
        if res.returncode == 0:
            return Response(res.stdout, mimetype='application/json')
        return jsonify({"error": res.stderr}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/worker/dvc/get', methods=['GET'])
def worker_dvc_get():
    repo = request.args.get('repo')
    rev = request.args.get('rev')
    file_path = request.args.get('path')
    if not repo or not file_path: return jsonify({"error": "Missing repo or path"}), 400

    repo_path = os.path.join(REPOS_DIR, repo)
    if not os.path.exists(repo_path):
        return jsonify({"error": "Repository not found on this worker"}), 404

    tmp_dir = tempfile.mkdtemp()
    try:
        # Ensure the requested revision is available locally
        if rev:
            subprocess.run(["git", "fetch", "origin"], cwd=repo_path,
                           capture_output=True, timeout=30)

        import mimetypes
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = 'application/octet-stream'
        disposition = "inline" if request.args.get("inline") == "true" else "attachment"

        # Strategy 1: DVC extraction at specific revision (historical integrity)
        cmd = [DVC_CMD, "get", ".", file_path, "--out", tmp_dir]
        if rev: cmd += ["--rev", rev]

        res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
        if res.returncode == 0:
            filename = os.path.basename(file_path)
            full_path = os.path.join(tmp_dir, filename)
            if os.path.exists(full_path) and os.path.isfile(full_path):
                def generate():
                    try:
                        with open(full_path, 'rb') as f:
                            while True:
                                chunk = f.read(4096)
                                if not chunk: break
                                yield chunk
                    finally:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                return Response(generate(), mimetype=mime_type,
                                headers={"Content-Disposition": f"{disposition}; filename=\"{filename}\""})

        shutil.rmtree(tmp_dir, ignore_errors=True)

        # Strategy 2: Direct filesystem fallback (P2P — file produced by dvc repro)
        # When no remote storage is configured, dvc get fails but the file
        # is already on disk from the last pipeline execution.
        direct_path = os.path.join(repo_path, file_path)
        if os.path.exists(direct_path) and os.path.isfile(direct_path):
            logger.info(f"[P2P] Serving {file_path} directly from working directory")
            return send_file(direct_path, as_attachment=(disposition == "attachment"),
                             mimetype=mime_type,
                             download_name=os.path.basename(file_path))

        return jsonify({"error": f"File not found via DVC or filesystem: {file_path}"}), 404

    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": str(e)}), 500

def _trigger_deferred_restart():
    """Schedule a systemd restart of cluster-worker in 5 seconds.
    
    Separated from update_self so it can be called either immediately
    (no job running) or deferred (after a job finishes).
    """
    global pending_update_restart
    with job_lock:
        pending_update_restart = False
    logger.info("Scheduling deferred restart of cluster-worker in 5 seconds...")
    subprocess.Popen(
        ["bash", "-c", "sleep 5 && sudo systemctl restart cluster-worker"],
        cwd="/tmp", start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

@app.route('/webhook/update_self', methods=['POST'])
def update_self():
    """GitOps auto-update endpoint.
    
    Pulls latest code from main, syncs dependencies, and schedules a deferred
    restart of the cluster-worker systemd service. The restart is deferred by 5s
    so this endpoint can return 202 before the process is killed.
    
    SAFETY: If a job is currently running, the code update (git pull + uv sync)
    still proceeds, but the restart is postponed until the job finishes. This
    prevents crash-loops from killing the worker mid-execution.
    """
    global pending_update_restart
    logger.info("Received update_self webhook — starting GitOps update")

    def _do_update():
        global pending_update_restart
        try:
            # 1. Pull latest code robustly using fetch + hard reset to bypass any local changes or merge conflicts
            subprocess.run(["git", "fetch", "origin", "main"], cwd=BASE_DIR, capture_output=True, timeout=60)
            res = subprocess.run(
                ["git", "reset", "--hard", "origin/main"],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=60
            )
            logger.info(f"git reset --hard: {res.stdout.strip()}")
            if res.returncode != 0:
                logger.error(f"git reset failed: {res.stderr}")
                return

            # 2. Sync dependencies (non-blocking if uv is available)
            uv_cmd = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
            if os.path.exists(uv_cmd):
                res = subprocess.run(
                    [uv_cmd, "sync"], cwd=BASE_DIR,
                    capture_output=True, text=True, timeout=120
                )
                logger.info(f"uv sync: {res.stdout.strip()}")

            # 3. Check if a job is running — if so, defer the restart
            with job_lock:
                job_active = current_job_id is not None
                if job_active:
                    pending_update_restart = True

            if job_active:
                logger.warning(
                    f"Job {current_job_id} is currently running — deferring restart until job completes. "
                    f"Code has been updated to latest commit; restart will trigger automatically after job finishes."
                )
            else:
                _trigger_deferred_restart()
        except Exception as e:
            logger.error(f"Update failed: {e}")

    threading.Thread(target=_do_update, daemon=True).start()
    return jsonify({"status": "accepted", "message": "Update in progress, restart scheduled"}), 202

@app.route('/webhook/drain_request', methods=['POST'])
def drain_request():
    logger.info("Received drain request webhook")
    # Run drain in a separate thread to avoid blocking the webhook response
    threading.Thread(target=drain_pending_syncs).start()
    return jsonify({"status": "accepted"})

def start_webhook_server():
    app.run(host='0.0.0.0', port=AGENT_PORT)

LOCK_FILE_PATH = os.path.join(tempfile.gettempdir(), "cluster-worker.lock")
lock_file = None
shutdown_requested = False

def acquire_single_instance_lock():
    global lock_file
    try:
        lock_file = open(LOCK_FILE_PATH, "w")
        if os.name != 'nt':
            import fcntl
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                logger.error(f"❌ CRITICAL ERROR: Another instance of worker_agent.py is already running on this host (locked via {LOCK_FILE_PATH}). Exiting immediately to prevent conflict.")
                sys.exit(1)
        else:
            import msvcrt
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except (IOError, OSError):
                logger.error(f"❌ CRITICAL ERROR: Another instance of worker_agent.py is already running on this host (locked via {LOCK_FILE_PATH}). Exiting immediately to prevent conflict.")
                sys.exit(1)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        logger.info(f"Successfully acquired single instance lock on {LOCK_FILE_PATH} (PID: {os.getpid()})")
    except Exception as e:
        logger.error(f"Error while acquiring single instance lock: {e}")
        if isinstance(e, SystemExit):
            raise e
        sys.exit(1)

def release_single_instance_lock():
    global lock_file
    if lock_file:
        try:
            if os.name != 'nt':
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
            if os.path.exists(LOCK_FILE_PATH):
                os.remove(LOCK_FILE_PATH)
            logger.info("Released single instance lock.")
        except Exception as e:
            logger.error(f"Error releasing lock file: {e}")

def cleanup_active_jobs_and_containers():
    global current_job_id, current_process
    
    # Ensure residual host Ollama VRAM is freed instantly during active job cleanup
    purge_ollama_vram_on_host()
    
    with job_lock:
        if current_job_id:
            logger.warning(f"🧹 Initiating forced cleanup for active job {current_job_id} due to shutdown request...")
            safe_job_id = current_job_id.replace('/', '-')
            for prefix in ["cluster-job-", "cluster-viewer-"]:
                container_name = f"{prefix}{safe_job_id}"
                logger.info(f"Force-removing container {container_name} on shutdown...")
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=10)
            
            if current_process:
                logger.info(f"Terminating local runner process (PID: {current_process.pid}) tree...")
                try:
                    parent = psutil.Process(current_process.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            
            try:
                logger.info(f"Notifying headnode of failure for job {current_job_id}...")
                update_job_status(current_job_id, 'failed', exit_code=-15)
            except Exception as e:
                logger.error(f"Failed to update job status on shutdown: {e}")

def signal_handler(signum, frame):
    global shutdown_requested
    signame = signal.Signals(signum).name
    logger.warning(f"⚠️ Received shutdown signal {signame} ({signum}). Starting graceful shutdown sequence...")
    shutdown_requested = True
    
    try:
        cleanup_active_jobs_and_containers()
    except Exception as e:
        logger.error(f"Error during active jobs cleanup: {e}")
        
    try:
        release_single_instance_lock()
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")
        
    logger.info("Graceful shutdown sequence complete. Exiting process.")
    sys.exit(0)

def register_signals():
    for sig in [signal.SIGTERM, signal.SIGINT]:
        try:
            signal.signal(sig, signal_handler)
            logger.info(f"Registered signal handler for {signal.Signals(sig).name}")
        except ValueError:
            pass
    if hasattr(signal, 'SIGHUP'):
        try:
            signal.signal(signal.SIGHUP, signal_handler)
            logger.info("Registered signal handler for SIGHUP")
        except ValueError:
            pass

# Background self-healing loop has been retired and replaced by deterministic JIT purges at job execution and worker startup.

def main_loop():
    # Enforce single instance lock first
    acquire_single_instance_lock()
    
    # Register signal handling for graceful shutdown
    register_signals()

    # R4. Worker Startup Docker & Process Reconciliation
    logger.info("Executing startup JIT Docker and process reconciliation to clean up any orphan/zombie state...")
    try:
        purge_orphan_runners_and_containers()
    except Exception as e:
        logger.error(f"Failed to perform startup reconciliation: {e}")

    # Start webhook server in background thread
    threading.Thread(target=start_webhook_server, daemon=True).start()
    
    # Start heartbeat in background thread
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    # Wait for the first heartbeat to be processed before polling for jobs
    if not startup_heartbeat_event.wait(timeout=300):
        logger.error("Timeout: Failed to synchronize initial heartbeat with headnode after 5 minutes. Shutting down worker.")
        release_single_instance_lock()
        sys.exit(1)

    try:
        while not shutdown_requested:
            job = poll_for_job()
            if job:
                execute_job(job)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt caught in main loop.")
    finally:
        release_single_instance_lock()

if __name__ == '__main__':
    main_loop()
