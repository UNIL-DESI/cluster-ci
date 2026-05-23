import requests
import os
import sys
import time
import argparse
import signal

def get_ram_requirement(repo=None, branch=None):
    """
    Reads RAM requirement from the .cluster-ci file.
    First tries to fetch the file from the remote repo (shallow clone),
    then falls back to reading from the current working directory.
    Expected format in .cluster-ci: --ram 16 or REQUIRED_RAM=16GB
    """
    content = None

    # Strategy 1: Fetch .cluster-ci from the remote repo
    if repo and branch:
        import tempfile, subprocess
        tmp_dir = tempfile.mkdtemp()
        try:
            gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_PAT")
            if gh_token:
                repo_url = f"https://x-access-token:{gh_token}@github.com/{repo}.git"
            else:
                repo_url = f"https://github.com/{repo}.git"
            subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, "--no-checkout", repo_url, tmp_dir],
                           check=True, capture_output=True, timeout=30)
            subprocess.run(["git", "checkout", f"origin/{branch}", "--", ".cluster-ci"],
                           cwd=tmp_dir, check=True, capture_output=True, timeout=10)
            ci_file = os.path.join(tmp_dir, ".cluster-ci")
            if os.path.exists(ci_file):
                with open(ci_file, 'r') as f:
                    content = f.read()
        except Exception as e:
            print(f"⚠️ Could not fetch .cluster-ci from {repo}@{branch}: {e}")
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # Strategy 2: Fallback to local CWD
    if content is None:
        if os.path.exists(".cluster-ci"):
            with open(".cluster-ci", 'r') as f:
                content = f.read()
        else:
            return 2.0  # Default 2GB

    import re
    # Try REQUIRED_RAM=16GB or REQUIRED_RAM=16.5
    match_env = re.search(r'REQUIRED_RAM\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    if match_env:
        return float(match_env.group(1))

    # Try --ram 16
    match = re.search(r'--ram\s+(\d+(?:\.\d+)?)', content)
    if match:
        return float(match.group(1))
    return 2.0  # Default

def get_config_value(pattern, content, default=None, is_float=False):
    import re
    match = re.search(pattern, content)
    if match:
        val = match.group(1)
        return float(val) if is_float else val
    return default

def submit_job(headnode_url, repo, branch, gh_token=None, env_vars=None, commit_hash=None):
    """Submits a research job to the headnode scheduler."""
    if not commit_hash:
        commit_hash = os.environ.get("CALLER_COMMIT_SHA") or os.environ.get("GITHUB_SHA")
        if not commit_hash:
            try:
                import subprocess
                commit_hash = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
            except Exception:
                pass
    # Strategy: Fetch .cluster-ci content first to parse all requirements
    content = None
    import tempfile, subprocess, shutil
    tmp_dir = tempfile.mkdtemp()
    try:
        gh_token_inner = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_PAT")
        if gh_token_inner:
            repo_url = f"https://x-access-token:{gh_token_inner}@github.com/{repo}.git"
        else:
            repo_url = f"https://github.com/{repo}.git"
        subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, "--no-checkout", repo_url, tmp_dir],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "checkout", f"origin/{branch}", "--", ".cluster-ci"],
                       cwd=tmp_dir, check=True, capture_output=True, timeout=10)
        ci_file = os.path.join(tmp_dir, ".cluster-ci")
        if os.path.exists(ci_file):
            with open(ci_file, 'r') as f:
                content = f.read()
    except Exception as e:
        print(f"⚠️ Could not fetch .cluster-ci from {repo}@{branch}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if content is None and os.path.exists(".cluster-ci"):
        with open(".cluster-ci", 'r') as f:
            content = f.read()

    if content is None:
        content = ""

    # Parse RAM
    import re
    ram_req = 2.0
    match_env = re.search(r'REQUIRED_RAM\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    if match_env:
        ram_req = float(match_env.group(1))
    else:
        match_ram = re.search(r'--ram\s+(\d+(?:\.\d+)?)', content)
        if match_ram:
            ram_req = float(match_ram.group(1))

    # Parse MAX_RUNTIME_HOURS (Fail-Fast)
    runtime_match = re.search(r'MAX_RUNTIME_HOURS\s*=\s*(\d+(?:\.\d+)?)', content)
    if not runtime_match:
        print("❌ Error: MAX_RUNTIME_HOURS is missing in .cluster-ci. This parameter is mandatory (max 24h).")
        sys.exit(1)

    max_runtime = float(runtime_match.group(1))
    if max_runtime <= 0 or max_runtime > 24:
        print(f"❌ Error: MAX_RUNTIME_HOURS must be between 0 and 24 hours (found: {max_runtime}).")
        sys.exit(1)

    # Parse EXPOSED_PORT
    exposed_port = None
    port_match = re.search(r'EXPOSED_PORT\s*=\s*(\d+)', content)
    if port_match:
        exposed_port = int(port_match.group(1))

    # Parse CUSTOM_WEB_APP
    custom_web_app = False
    custom_app_match = re.search(r'CUSTOM_WEB_APP\s*=\s*(true|1)', content, re.IGNORECASE)
    if custom_app_match:
        custom_web_app = True

    print(f"🚀 Submitting job for {repo}@{branch} (RAM: {ram_req}GB, Timeout: {max_runtime}h, Custom App: {custom_web_app})")

    token = os.environ.get("CLUSTER_TOKEN")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(f"{headnode_url}/submit_job", json={
            "repo": repo,
            "branch": branch,
            "commit_hash": commit_hash,
            "ram_required_gb": ram_req,
            "max_runtime_hours": max_runtime,
            "exposed_port": exposed_port,
            "custom_web_app": custom_web_app,
            "gh_run_id": os.environ.get("GITHUB_RUN_ID"),
            "gh_token": gh_token,
            "env_vars": env_vars,
            "username": os.environ.get("GITHUB_ACTOR", "unknown")
        }, headers=headers, timeout=10)
        resp.raise_for_status()
        job_data = resp.json()
        job_id = job_data['job_id']
        print(f"✅ Job submitted successfully! ID: {job_id}")
        return job_id
    except Exception as e:
        print(f"❌ Failed to submit job: {e}")
        sys.exit(1)

def wait_for_job(headnode_url, job_id):
    """Polls the headnode for job status and streams logs from the worker."""
    print(f"⏳ Waiting for job {job_id} to complete...")

    def signal_handler(sig, frame):
        worker_url = None
        cancel_error = None
        try:
            resp = requests.get(f"{headnode_url}/job_status/{job_id}", timeout=10)
            resp.raise_for_status()
            job = resp.json()
            worker_url = job.get('worker_service_url')

            if worker_url:
                requests.post(f"{worker_url}/cancel/{job_id}", timeout=10)

            # Mark job as failed/cancelled on headnode
            token = os.environ.get("CLUSTER_TOKEN")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            requests.post(f"{headnode_url}/update_job_status", json={
                "job_id": job_id,
                "status": "failed",
                "exit_code": -signal.SIGTERM
            }, headers=headers, timeout=10)

        except Exception as e:
            cancel_error = e

        try:
            print(f"\n🛑 Signal received ({signal.Signals(sig).name}). Propagating cancellation...")
            if cancel_error:
                print(f"⚠️ Error during cancellation: {cancel_error}")
            else:
                if worker_url:
                    print(f"📡 Sending cancellation to worker: {worker_url}")
                    print("✅ Cancellation signal sent.")
                else:
                    print("⚠️ Job was not yet assigned to a worker or worker info missing.")
        except Exception:
            pass

        sys.exit(128 + sig)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log_offset = 0
    status_printed = False
    oom_detected = False
    last_queue_check = 0

    while True:
        try:
            resp = requests.get(f"{headnode_url}/job_status/{job_id}", timeout=10)
            resp.raise_for_status()
            job = resp.json()
            status = job['status']
            worker_url = job.get('worker_service_url')
            ram_required = job.get('ram_required_gb', 2.0)

            if status == 'pending':
                now = time.time()
                if now - last_queue_check >= 10:
                    last_queue_check = now
                    try:
                        status_resp = requests.get(f"{headnode_url}/scheduler_status", timeout=5)
                        if status_resp.status_code == 200:
                            data = status_resp.json()
                            workers = data.get("workers", [])
                            queue = data.get("queue", [])
                            
                            # 1. Find own position in queue
                            own_position = -1
                            for idx, q_job in enumerate(queue):
                                if q_job["job_id"] == job_id:
                                    own_position = idx + 1
                                    break
                            
                            # 2. Check physical RAM capacity of the cluster
                            online_workers = [w for w in workers if w["status"] == "online"]
                            max_ram = max([w["total_ram_gb"] for w in online_workers]) if online_workers else 0.0
                            
                            # Filter compatible workers physically capable of running the job
                            compatible_workers = [w for w in online_workers if (w["total_ram_gb"] - 2.0) >= ram_required]
                            
                            # Format details
                            print("\n" + "═"*55)
                            print(f"⏳ FILE D'ATTENTE CLUSTER-CI (Job: {job_id[:8]})")
                            if own_position != -1:
                                print(f"   👉 Position dans la file : {own_position} / {len(queue)}")
                            else:
                                print(f"   👉 Position dans la file : En cours d'analyse par le scheduler...")
                            
                            # Diagnostic if RAM required exceeds maximum physical capacity in the cluster
                            if online_workers and ram_required > (max_ram - 2.0):
                                print(f"   ⚠️  CRITIQUE : Votre tâche demande {ram_required:.1f} GB de RAM.")
                                print(f"      Mais la capacité maximale des machines en ligne (moins 2GB de marge OS) est de {max_ram - 2.0:.1f} GB.")
                                print(f"      Ce job ne pourra JAMAIS démarrer ! Veuillez baisser REQUIRED_RAM dans .cluster-ci.")
                            elif online_workers and not compatible_workers:
                                print(f"   ⚠️  ATTENTE : Aucune machine actuellement en ligne ne dispose d'assez de RAM physique ({ram_required:.1f} GB requis).")
                                print(f"      En attente qu'un worker avec une capacité suffisante vienne s'enregistrer.")
                            elif online_workers and compatible_workers:
                                # Check if all compatible workers are busy
                                all_busy = all([w.get("active_job") is not None for w in compatible_workers])
                                if all_busy:
                                    print(f"   ⚠️  ATTENTE : Toutes les machines compatibles avec vos besoins en RAM ({ram_required:.1f} GB) sont occupées.")
                                    
                                    # Calculate remaining times
                                    remaining_times = []
                                    for w in compatible_workers:
                                        active_job = w["active_job"]
                                        started_at = active_job.get("started_at")
                                        max_hours = active_job.get("max_runtime_hours", 24.0) or 24.0
                                        if started_at:
                                            try:
                                                from datetime import datetime
                                                import datetime as dt
                                                start_t = datetime.strptime(started_at.split(".")[0], "%Y-%m-%d %H:%M:%S")
                                                now_utc = dt.datetime.utcnow()
                                                diff = now_utc - start_t
                                                elapsed_secs = diff.total_seconds()
                                                max_secs = max_hours * 3600
                                                rem_secs = max(0.0, max_secs - elapsed_secs)
                                                remaining_times.append(rem_secs)
                                            except Exception:
                                                pass
                                                
                                    if remaining_times:
                                        if own_position == 1:
                                            min_rem = min(remaining_times)
                                            rem_mins, rem_secs = divmod(min_rem, 60)
                                            rem_hours, rem_mins = divmod(rem_mins, 60)
                                            if rem_hours > 0:
                                                time_str = f"{int(rem_hours)}h {int(rem_mins)}m"
                                            else:
                                                time_str = f"{int(rem_mins)}m {int(rem_secs)}s"
                                            print(f"      👉 Temps d'attente maximum estimé : ~{time_str} (dès que le premier worker compatible se libère)")
                                        else:
                                            print(f"      👉 Temps d'attente : Estimé après libération et traitement de {own_position - 1} job(s) devant vous.")
                            
                            # Current running jobs details on each machine
                            print("\n   🖥️  Statut des machines du cluster :")
                            if not online_workers:
                                print("      ❌ Aucune machine n'est actuellement en ligne ou active.")
                            else:
                                for w in online_workers:
                                    active_job = w.get("active_job")
                                    is_compatible = (w["total_ram_gb"] - 2.0) >= ram_required
                                    comp_str = "Compatible" if is_compatible else "RAM insuffisante"
                                    
                                    if active_job:
                                        duration_str = "en cours"
                                        remaining_str = "indéterminé"
                                        started_at = active_job.get("started_at")
                                        max_hours = active_job.get("max_runtime_hours", 24.0) or 24.0
                                        
                                        if started_at:
                                            try:
                                                from datetime import datetime
                                                import datetime as dt
                                                start_t = datetime.strptime(started_at.split(".")[0], "%Y-%m-%d %H:%M:%S")
                                                now_utc = dt.datetime.utcnow()
                                                diff = now_utc - start_t
                                                elapsed_secs = diff.total_seconds()
                                                
                                                # Elapsed time format
                                                mins, secs = divmod(elapsed_secs, 60)
                                                hours, mins = divmod(mins, 60)
                                                if hours > 0:
                                                    duration_str = f"{int(hours)}h {int(mins)}m"
                                                else:
                                                    duration_str = f"{int(mins)}m {int(secs)}s"
                                                    
                                                # Remaining time format
                                                max_secs = max_hours * 3600
                                                rem_secs = max(0.0, max_secs - elapsed_secs)
                                                rem_mins, rem_secs = divmod(rem_secs, 60)
                                                rem_hours, rem_mins = divmod(rem_mins, 60)
                                                if rem_hours > 0:
                                                    remaining_str = f"{int(rem_hours)}h {int(rem_mins)}m max"
                                                else:
                                                    remaining_str = f"{int(rem_mins)}m max"
                                            except Exception:
                                                pass
                                                
                                        print(f"      ● Machine {w['hostname']} ({comp_str}) : OCCUPÉE | Tâche [{active_job['repo'].split('/')[-1]}] par [{active_job['username']}] (depuis {duration_str}, reste au max {remaining_str} | utilise {active_job['ram_required_gb']:.1f} GB)")
                                    else:
                                        print(f"      ○ Machine {w['hostname']} ({comp_str}) : LIBRE | RAM Physique : {w['total_ram_gb']:.1f} GB")
                                        
                            # Waiting queue list
                            if len(queue) > 1:
                                print("\n   📋 Jobs en attente devant vous :")
                                count = 0
                                for q_job in queue:
                                    if q_job["job_id"] == job_id:
                                        break
                                    count += 1
                                    if count <= 3:
                                        print(f"      #{count} : Job [{q_job['repo'].split('/')[-1]}] par [{q_job['username']}] (demande {q_job['ram_required_gb']:.1f} GB)")
                                if len(queue) - 1 > count:
                                    print(f"      ... et {len(queue) - 1 - count} autre(s) job(s)")
                                    
                            print("═"*55 + "\n")
                    except Exception as e:
                        # Fallback silently to prevent blocking the execution loop
                        pass

            if worker_url:
                try:
                    logs_resp = requests.get(f"{worker_url}/job_logs/{job_id}?offset={log_offset}", timeout=5)
                    if logs_resp.status_code == 200:
                        logs_data = logs_resp.json()
                        new_logs = logs_data.get('logs', '')
                        if new_logs:
                            import re
                            if re.search(r'Exit code 137|OOM|Out of Memory|exited with -9', new_logs, re.IGNORECASE):
                                oom_detected = True
                            if not status_printed:
                                print(f"\n\n[Streaming logs from {worker_url}]")
                                status_printed = True
                            sys.stdout.write(new_logs)
                            sys.stdout.flush()
                            log_offset = logs_data.get('offset', log_offset)
                except Exception as e:
                    pass

            if status == 'completed':
                print(f"\n✅ Job {job_id} completed successfully!")
                return 0
            elif status == 'failed':
                exit_code = job.get('exit_code')
                if exit_code is None or exit_code == 0:
                    exit_code = 1  # Ensure non-zero exit on failure

                # Infrastructure-level failure messages
                if exit_code == -99:
                    print(f"\n❌ Job {job_id} failed: Worker became unreachable (timeout/offline). The job was orphaned.")
                elif exit_code == -98:
                    print(f"\n❌ Job {job_id} failed: Worker restarted while the job was running/assigned. (OOM or System Crash)")
                    if worker_url:
                        try:
                            crash_resp = requests.get(f"{worker_url}/crash_report", timeout=5)
                            if crash_resp.status_code == 200:
                                dmesg = crash_resp.json().get('dmesg', '').strip()
                                if dmesg:
                                    print("\n--- SYSTEM CRASH REPORT (dmesg) ---")
                                    print(dmesg)
                                    print("-----------------------------------")
                        except Exception:
                            pass
                elif exit_code == 137 or oom_detected:
                    print(f"\n❌ Erreur: Le job a dépassé la limite REQUIRED_RAM allouée ({ram_required} GB) et a été tué par le système (OOM Killer). Veuillez augmenter cette limite dans le fichier .cluster-ci")
                else:
                    print(f"\n❌ Job {job_id} failed with exit code {exit_code}")
                return exit_code

            if not status_printed:
                sys.stdout.write(f"\rStatus: {status}... ")
                sys.stdout.flush()

        except Exception as e:
            print(f"\n⚠️ Error checking status: {e}")

        time.sleep(2)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Submit a job to Cluster-CI Scheduler")
    parser.add_argument("repo", help="Target repository (owner/repo)")
    parser.add_argument("branch", help="Target branch")
    parser.add_argument("--headnode", default=os.environ.get("HEADNODE_URL", "http://localhost:5000"), help="Headnode URL")
    parser.add_argument("--gh-token", default=None, help="GitHub token for cloning private repos")
    parser.add_argument("-e", "--env", action="append", help="Environment variables to pass (KEY=VALUE)", default=[])

    args = parser.parse_args()

    env_vars = {}
    
    # Process explicit -e flags
    for e in args.env:
        if "=" in e:
            k, v = e.split("=", 1)
            env_vars[k] = v

    # Process automatic GitHub Secrets injection
    all_secrets_json = os.environ.get("ALL_GITHUB_SECRETS")
    if all_secrets_json:
        try:
            import json
            secrets_dict = json.loads(all_secrets_json)
            for k, v in secrets_dict.items():
                if k.lower() != 'github_token':
                    env_vars[k] = v
        except Exception as e:
            print(f"⚠️ Failed to parse ALL_GITHUB_SECRETS: {e}")

    job_id = submit_job(args.headnode, args.repo, args.branch, args.gh_token, env_vars)
    exit_code = wait_for_job(args.headnode, job_id)
    sys.exit(exit_code)
