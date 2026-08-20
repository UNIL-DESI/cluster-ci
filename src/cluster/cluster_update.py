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
        "--force",
        action="store_true",
        help="Bypass queue and execute immediate direct SSH update.",
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
    queue_mode = not force_mode

    env_vars = load_env_vars()
    target_repo = args.target_repo or env_vars.get("TARGET_REPO") or "UNIL-DESI"

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
