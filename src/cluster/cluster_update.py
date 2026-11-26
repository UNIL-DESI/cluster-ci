#!/usr/bin/env python3
"""Cluster-CI Update CLI

Submits a maintenance job with a drainage barrier to the Headnode API,
or forces an immediate emergency direct SSH update.
Compatible with Windows, macOS, and Linux.
"""

import sys
import os
import time
import json
import argparse
import subprocess
import shutil
import urllib.request
import urllib.error

# Configure UTF-8 stdout if available
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

def find_env_file():
    """Locate .env file in current or parent directories."""
    cwd = os.getcwd()
    for _ in range(4):
        candidate = os.path.join(cwd, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent
    return None

def load_env_vars():
    """Load environment variables from .env file without modifying system env."""
    vars_dict = {}
    env_path = find_env_file()
    if env_path and os.path.isfile(env_path):
        try:
            with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    vars_dict[key] = val
        except Exception:
            pass
    return vars_dict

def discover_headnode_url(args_url=None, env_vars=None):
    """Discover headnode URL from args, environment, or .env."""
    if args_url:
        return args_url.rstrip("/")
    if os.environ.get("HEADNODE_URL"):
        return os.environ.get("HEADNODE_URL").rstrip("/")
    if env_vars and env_vars.get("HEADNODE_URL"):
        return env_vars["HEADNODE_URL"].rstrip("/")
    if os.environ.get("HEADNODE_IP"):
        return f"http://{os.environ.get('HEADNODE_IP')}:5000"
    if env_vars and env_vars.get("HEADNODE_IP"):
        return f"http://{env_vars['HEADNODE_IP']}:5000"
    return "http://localhost:5000"

def discover_token(args_token=None, env_vars=None):
    """Discover CLUSTER_TOKEN from args, environment, or .env."""
    if args_token:
        return args_token
    if os.environ.get("CLUSTER_TOKEN"):
        return os.environ.get("CLUSTER_TOKEN")
    if env_vars and env_vars.get("CLUSTER_TOKEN"):
        return env_vars["CLUSTER_TOKEN"]
    return None

def submit_maintenance_job(headnode_url, token, target_repo, branch, description, username=None):
    """Submit maintenance barrier job to the Headnode API."""
    url = f"{headnode_url}/submit_maintenance_job"
    payload = {
        "target_repo": target_repo,
        "branch": branch,
        "username": username or os.environ.get("USER") or os.environ.get("USERNAME") or "admin",
        "description": description,
        "max_runtime_hours": 1.0,
    }
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode("utf-8")
            return json.loads(resp_body)
    except urllib.error.HTTPError as he:
        err_body = he.read().decode("utf-8", errors="replace")
        print(f"❌ HTTP Error {he.code} submitting maintenance job: {err_body}", file=sys.stderr)
        return None
    except Exception as ex:
        print(f"❌ Connection error submitting maintenance job to {headnode_url}: {ex}", file=sys.stderr)
        return None

def poll_maintenance_job(headnode_url, token, job_id, timeout_seconds=900):
    """Poll job status until completion, printing status transitions."""
    url = f"{headnode_url}/job_status/{job_id}"
    last_status = None
    start_time = time.time()

    print(f"\n📡 Tracking maintenance job {job_id} on {headnode_url}...")
    
    while time.time() - start_time < timeout_seconds:
        try:
            req = urllib.request.Request(url)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                status = data.get("status")
                exit_code = data.get("exit_code")

                if status != last_status:
                    last_status = status
                    elapsed = int(time.time() - start_time)
                    if status == "pending":
                        print(f"⏳ [{elapsed}s] Barrière de drainage active : attente de la fin des jobs de calcul sur les workers...")
                    elif status == "running":
                        print(f"🛠️  [{elapsed}s] Nœuds drainés : Déploiement et mise à jour des conteneurs/services en cours...")
                    elif status == "completed":
                        print(f"\n🎉 [{elapsed}s] Mise à jour du cluster terminée avec succès ! Tous les nœuds sont opérationnels.")
                        return 0
                    elif status == "failed":
                        print(f"\n❌ [{elapsed}s] La mise à jour a échoué (exit code: {exit_code}).")
                        return exit_code if exit_code is not None else 1

        except Exception as ex:
            # Tolerant to temporary network blips during headnode/worker restart
            print(f"🔄 Reconnecting to headnode ({ex})...", end="\r", flush=True)

        time.sleep(4)

    print(f"\n⚠️ Timeout waiting for maintenance job {job_id} after {timeout_seconds}s.", file=sys.stderr)
    return 2

def _run_ssh_command(host, username, password, remote_cmd, sudo_password=None, timeout=180):
    """Execute a shell command over SSH using paramiko (cross-platform, zero sshpass required)."""
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError("paramiko is required for --hot SSH updates (pip install paramiko)") from exc

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=username, password=password, timeout=15, banner_timeout=15)
    try:
        stdin, stdout, stderr = client.exec_command(remote_cmd, timeout=timeout)
        if sudo_password is not None:
            # Passed cleanly via stdin for read -r SUDO_PASSWORD (never exposed in ps/argv)
            stdin.write(sudo_password + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    finally:
        client.close()


def _build_hot_update_remote_script(role, branch):
    """Build remote bash script for zero-disruption hot update (never kills running jobs)."""
    is_headnode = (role == "headnode")
    headnode_block = """
echo "🔄 [HOT-UPDATE] Restarting control plane (cluster-scheduler & cluster-scheduler-loop)..."
sudo_run systemctl daemon-reload
sudo_run systemctl restart cluster-scheduler
sudo_run systemctl restart cluster-scheduler-loop
if pgrep -f "submit_job.py" >/dev/null 2>&1; then
    echo "ℹ️  Active GHA delegation detected; keeping cluster-runner-manager alive."
else
    sudo_run systemctl restart cluster-runner-manager || true
fi
""" if is_headnode else ""

    after_line = "After=network.target cluster-scheduler.service" if is_headnode else "After=network.target"

    return f"""set -e
IFS= read -r SUDO_PW || true
sudo_run() {{
    printf '%s\\n' "$SUDO_PW" | sudo -S -p '' "$@"
}}

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
BASE_DIR="$HOME/cluster-ci"
cd "$BASE_DIR"

echo "📂 Updating $BASE_DIR to origin/{branch}..."
git fetch origin
git reset --hard "origin/{branch}"
uv pip install -e "$BASE_DIR" --no-deps

# Ensure global orchestrator symlink points to updated script
sudo_run ln -sf "$BASE_DIR/src/runner/run_research_pipeline.sh" /usr/local/bin/cluster-ci-run

# Snapshot currently active cluster-worker unit BEFORE editing it
PREV_KILLMODE=$(systemctl show -p KillMode --value cluster-worker 2>/dev/null || echo "control-group")
PREV_STOPPOST=$(systemctl show -p ExecStopPost --value cluster-worker 2>/dev/null || echo "")

# Rewrite cluster-worker.service cleanly with KillMode=process (no destructive ExecStopPost)
PY_BIN=$(uv python find)
if systemctl list-unit-files cluster-worker.service >/dev/null 2>&1 && [ -f /etc/systemd/system/cluster-worker.service ]; then
    UNIT_TMP=$(mktemp)
    cat <<EOF_UNIT > "$UNIT_TMP"
[Unit]
Description=Cluster-CI Worker Agent
{after_line}
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
KillMode=process
User=$USER
WorkingDirectory=$BASE_DIR
EnvironmentFile=$BASE_DIR/.env
Environment="PATH=$BASE_DIR/.venv/bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
ExecStart=$PY_BIN $BASE_DIR/src/scheduler/worker_agent.py
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF_UNIT
    sudo_run cp "$UNIT_TMP" /etc/systemd/system/cluster-worker.service
    sudo_run chmod 644 /etc/systemd/system/cluster-worker.service
    rm -f "$UNIT_TMP"
    sudo_run systemctl daemon-reload
fi
{headnode_block}
if systemctl is-enabled cluster-worker >/dev/null 2>&1 || systemctl is-active cluster-worker >/dev/null 2>&1; then
    ACTIVE_CONTAINERS=$( (docker ps -q --filter name=cluster-job- 2>/dev/null || sudo_run docker ps -q --filter name=cluster-job- 2>/dev/null || true) | wc -l | tr -d ' ')
    ACTIVE_RUNNERS=$( (pgrep -f "branch_executor\\.py|CLUSTER_CI_MODE=executor|run_research_pipeline\\.sh" 2>/dev/null || true) | wc -l | tr -d ' ')
    if [ "${{ACTIVE_CONTAINERS:-0}}" -eq 0 ] && [ "${{ACTIVE_RUNNERS:-0}}" -eq 0 ]; then
        echo "🚀 [HOT-UPDATE] Worker is idle; restarting cluster-worker immediately..."
        rm -f /tmp/cluster-worker-pending-restart
        sudo_run systemctl restart cluster-worker
    elif [ ! -f /tmp/cluster-worker-pending-restart ] && [ "$PREV_KILLMODE" = "process" ] && ! echo "$PREV_STOPPOST" | grep -q "docker rm"; then
        echo "🔄 [HOT-UPDATE] Hot-reloading cluster-worker while preserving $ACTIVE_CONTAINERS container(s) / $ACTIVE_RUNNERS runner(s)..."
        touch /tmp/cluster-worker-hot-reload
        sudo_run systemctl restart cluster-worker
    else
        echo "⏳ [HOT-UPDATE] Legacy cluster-worker has active workload ($ACTIVE_CONTAINERS container(s), $ACTIVE_RUNNERS runner(s)); code & KillMode=process unit updated on disk, worker restart deferred until idle (ZERO running jobs interrupted)."
        touch /tmp/cluster-worker-pending-restart
    fi
fi
echo "✅ Hot update complete on $(hostname) ($BASE_DIR @ $(git rev-parse --short HEAD))."
"""


def execute_hot_ssh(args, env_vars):
    """Execute zero-disruption hot update across Headnode and all Workers without cancelling running jobs."""
    headnode_ip = env_vars.get("HEADNODE_IP") or os.environ.get("HEADNODE_IP")
    headnode_user = env_vars.get("HEADNODE_USER") or os.environ.get("HEADNODE_USER")
    headnode_pass = env_vars.get("HEADNODE_PASS") or os.environ.get("HEADNODE_PASS")
    branch = args.branch or "main"

    if not headnode_ip or not headnode_user or not headnode_pass:
        print("❌ Error: HEADNODE_IP, HEADNODE_USER, and HEADNODE_PASS must be set in .env for --hot updates.", file=sys.stderr)
        return 1

    print("===========================================================")
    print(f"🔥 Mode HOT : Déploiement sans interruption (branch: {branch})")
    print("===========================================================")

    # 1. Update Headnode (control plane restarts in ~2s; running worker jobs untouched)
    print(f"\n🚀 [1] Hot-updating Headnode ({headnode_user}@{headnode_ip})...")
    rc, out, err = _run_ssh_command(
        headnode_ip, headnode_user, headnode_pass,
        _build_hot_update_remote_script("headnode", branch),
        sudo_password=headnode_pass,
    )
    if out.strip():
        print(out.strip())
    if rc != 0:
        print(f"❌ Headnode hot-update failed (exit {rc}):\n{err}", file=sys.stderr)
        return rc or 1

    # 2. Discover & update all distinct workers in .env
    worker_indices = sorted({
        int(k.split("_")[1])
        for k in env_vars
        if k.startswith("WORKER_") and k.endswith("_IP") and k.split("_")[1].isdigit()
    })
    for idx in worker_indices:
        w_ip = env_vars.get(f"WORKER_{idx}_IP")
        w_user = env_vars.get(f"WORKER_{idx}_USER")
        w_pass = env_vars.get(f"WORKER_{idx}_PASS")
        if not w_ip or not w_user or not w_pass:
            continue
        if w_ip == headnode_ip:
            print(f"\nℹ️  Worker {idx} ({w_ip}) is Headnode (already updated).")
            continue
        print(f"\n🚀 [Worker {idx}] Hot-updating {w_user}@{w_ip}...")
        try:
            w_rc, w_out, w_err = _run_ssh_command(
                w_ip, w_user, w_pass,
                _build_hot_update_remote_script("worker", branch),
                sudo_password=w_pass,
            )
            if w_out.strip():
                print(w_out.strip())
            if w_rc != 0:
                print(f"⚠️  Worker {idx} ({w_ip}) hot-update failed (exit {w_rc}): {w_err}", file=sys.stderr)
        except Exception as exc:
            print(f"⚠️  Worker {idx} ({w_ip}) unreachable: {exc}", file=sys.stderr)

    # 3. Verify Headnode API & live supervision resumed cleanly
    headnode_url = discover_headnode_url(args.headnode_url, env_vars)
    token = discover_token(args.token, env_vars)
    print(f"\n🔍 Verifying Headnode API & live job supervision at {headnode_url}...")
    for attempt in range(1, 16):
        try:
            req = urllib.request.Request(f"{headnode_url}/scheduler_status")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    workers = data.get("workers", [])
                    queue_items = data.get("queue", [])
                    active_jobs = [w["active_job"]["job_id"][:8] for w in workers if isinstance(w.get("active_job"), dict)]
                    print(f"✅ Headnode online! Workers: {len(workers)} online | Active jobs preserved: {active_jobs} | Queue: {len(queue_items)}")
                    return 0
        except Exception:
            time.sleep(2)
    print("⚠️  Headnode did not respond on /scheduler_status within 30s.", file=sys.stderr)
    return 1


def execute_force_ssh(args, env_vars):
    """Execute direct emergency SSH update script."""
    print("===========================================================")
    print("⚡ Mode FORCE : Exécution immédiate de la mise à jour SSH...")
    print("===========================================================")
    
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sh_script = os.path.join(base_dir, "update_cluster.sh")
    
    if not os.path.isfile(sh_script):
        if os.path.isfile("update_cluster.sh"):
            sh_script = os.path.abspath("update_cluster.sh")
        else:
            print(f"❌ Could not find update_cluster.sh at {sh_script}", file=sys.stderr)
            return 1

    bash_bin = shutil.which("bash") or "bash"
    cmd = [bash_bin, sh_script, "--force"]
    if args.add_worker:
        cmd.append("--add-worker")

    print(f"🚀 Running: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, cwd=os.path.dirname(sh_script))
        return res.returncode
    except Exception as ex:
        print(f"❌ Failed to run {sh_script}: {ex}", file=sys.stderr)
        return 1

def main():
    parser = argparse.ArgumentParser(
        description="Cluster-CI Update CLI: Managed drainage barrier or emergency force update."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--queue",
        action="store_true",
        default=True,
        help="Queue maintenance job with drainage barrier (default).",
    )
    group.add_argument(
        "--hot",
        action="store_true",
        help="Zero-disruption hot update over SSH: restarts control plane (~2s) and hot-reloads/defers workers without cancelling running jobs.",
    )
    group.add_argument(
        "--force",
        action="store_true",
        help="Bypass queue and execute immediate direct SSH update (kills active worker containers).",
    )
    parser.add_argument(
        "--target-repo",
        default=None,
        help="Target repository or organization (e.g., UNIL-DESI).",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Target branch for cluster-ci repository (default: main).",
    )
    parser.add_argument(
        "--headnode-url",
        default=None,
        help="Headnode URL override (e.g., http://130.223.73.209:5000).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Cluster Token override.",
    )
    parser.add_argument(
        "--description",
        default="Cluster Upgrade: Docker images & worker agents",
        help="Description of maintenance operation.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Submit maintenance job and exit immediately without polling.",
    )
    parser.add_argument(
        "--add-worker",
        action="store_true",
        help="Interactive worker addition mode (used in force mode).",
    )

    args = parser.parse_args()

    force_mode = args.force
    hot_mode = args.hot

    env_vars = load_env_vars()
    target_repo = args.target_repo or env_vars.get("TARGET_REPO") or "UNIL-DESI"

    if hot_mode:
        code = execute_hot_ssh(args, env_vars)
        sys.exit(code)

    if force_mode:
        code = execute_force_ssh(args, env_vars)
        sys.exit(code)

    headnode_url = discover_headnode_url(args.headnode_url, env_vars)
    token = discover_token(args.token, env_vars)

    print("===========================================================")
    print("🛠️  Cluster-CI: Maintenance Job Submission (Drainage Barrier)")
    print("===========================================================")
    print(f"🎯 Target Repo : {target_repo}/cluster-ci (branch: {args.branch})")
    print(f"🔗 Headnode URL : {headnode_url}")
    print(f"📝 Description : {args.description}")
    print("===========================================================")

    result = submit_maintenance_job(
        headnode_url=headnode_url,
        token=token,
        target_repo=target_repo,
        branch=args.branch,
        description=args.description,
    )

    if not result or not result.get("job_id"):
        print("❌ Failed to queue maintenance job on headnode.", file=sys.stderr)
        sys.exit(1)

    job_id = result["job_id"]
    print(f"✅ Maintenance barrier registered! Job ID: {job_id}")
    print("💡 The scheduler will freeze new compute jobs and wait for running tasks to finish.")

    if args.no_wait:
        print(f"👉 You can monitor progress on the dashboard: {headnode_url}")
        sys.exit(0)

    exit_code = poll_maintenance_job(headnode_url, token, job_id)
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
