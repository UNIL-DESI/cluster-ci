import requests
import os
import sys
import time
import argparse
import signal

# Source de vérité des défauts v3 (spec_v3_interfaces §1, W1 src/config/defaults.py)
try:
    from src.config.defaults import (
        DEFAULT_RESOURCES,
        DEFAULT_CPUS,
        DEFAULT_GPUS,
        DEFAULT_STORAGE_GB,
    )
    DEFAULT_RAM_GB = float(DEFAULT_RESOURCES["ram_gb"])
except ImportError:
    try:
        from config.defaults import (
            DEFAULT_RESOURCES,
            DEFAULT_CPUS,
            DEFAULT_GPUS,
            DEFAULT_STORAGE_GB,
        )
        DEFAULT_RAM_GB = float(DEFAULT_RESOURCES["ram_gb"])
    except ImportError:
        try:
            from scheduler.defaults import DEFAULT_RAM_GB
            DEFAULT_CPUS = 2
            DEFAULT_GPUS = 0
            DEFAULT_STORAGE_GB = 0.0
        except ImportError:
            DEFAULT_RAM_GB = 10.0
            DEFAULT_CPUS = 2
            DEFAULT_GPUS = 0
            DEFAULT_STORAGE_GB = 0.0


def get_planner_module_name():
    """Détecte ou retourne le nom du module planificateur fourni par W1."""
    env_mod = os.environ.get("CLUSTER_CI_PLANNER_MODULE")
    if env_mod:
        return env_mod
    candidates = [
        ("src.planner.stage_plan", "src/planner/stage_plan.py"),
        ("src.scheduler.planner", "src/scheduler/planner.py"),
        ("scheduler.planner", "scheduler/planner.py"),
    ]
    for mod_name, file_rel in candidates:
        if os.path.exists(file_rel) or os.path.exists(
            os.path.join(os.path.dirname(__file__), "..", "..", file_rel)
        ):
            return mod_name
    return "src.planner.stage_plan"


def run_planner_for_submission(repo_dir="."):
    """Exécute le planificateur W1 via CLI (python -m <module> --repo <repo_dir> --json).

    Utilise DVC 3.67.1 via uv/uvx quand disponible, ou l'interpréteur Python actif.
    En cas d'erreur (clé inconnue sous meta.cluster, etc.), échoue immédiatement
    avec le message exact et ne se replie JAMAIS silencieusement.
    """
    import json
    import shutil
    import subprocess

    planner_mod = get_planner_module_name()
    target_repo = os.path.abspath(repo_dir)

    # Commande CLI : privilégier uv run avec dvc==3.67.1 si uv est installé
    uv_path = shutil.which("uv")
    if uv_path:
        cmd = [
            uv_path,
            "run",
            "--with",
            "dvc==3.67.1",
            "python",
            "-m",
            planner_mod,
            "--repo",
            target_repo,
            "--json",
        ]
    else:
        cmd = [sys.executable, "-m", planner_mod, "--repo", target_repo, "--json"]

    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    paths = [
        target_repo,
        os.path.dirname(os.path.abspath(__file__)),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    ]
    env["PYTHONPATH"] = os.pathsep.join(paths + ([pythonpath] if pythonpath else []))

    try:
        proc = subprocess.run(
            cmd,
            cwd=target_repo,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as e:
        print(f"❌ Error: Impossible d'exécuter le planificateur ({' '.join(cmd)}): {e}", file=sys.stderr)
        sys.exit(1)

    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or "").strip()
        print(
            f"❌ Error: Échec du planificateur (code {proc.returncode}):\n{err_msg}",
            file=sys.stderr,
        )
        sys.exit(proc.returncode if proc.returncode != 0 else 1)

    output = proc.stdout.strip()
    try:
        plan_data = json.loads(output)
        return plan_data
    except json.JSONDecodeError as e:
        print(
            f"❌ Error: Sortie JSON invalide du planificateur: {e}\nSortie brute:\n{output}",
            file=sys.stderr,
        )
        sys.exit(1)


def format_nodes_status_summary(nodes):
    """Génère un résumé textuel et structuré de l'état des nœuds du DAG.

    Accepte une liste de dictionnaires, un dictionnaire de nœuds, ou un dictionnaire englobant.
    """
    if not nodes:
        return None, []

    node_list = []
    if isinstance(nodes, dict):
        if "nodes" in nodes and isinstance(nodes["nodes"], list):
            node_list = nodes["nodes"]
        else:
            for k, v in nodes.items():
                if isinstance(v, dict):
                    node_list.append({"name": k, **v})
                else:
                    node_list.append({"name": k, "status": str(v)})
    elif isinstance(nodes, list):
        node_list = nodes

    if not node_list:
        return None, []

    counts = {
        "done": 0,
        "running": 0,
        "ready": 0,
        "failed": 0,
        "blocked": 0,
        "pending": 0,
        "skipped": 0,
    }

    running_nodes = []
    failed_nodes = []
    blocked_nodes = []

    for n in node_list:
        name = n.get("name") or n.get("node_name") or "unknown"
        st = (n.get("status") or "pending").lower()
        worker = n.get("worker_id") or n.get("worker")

        if st in ("done", "completed"):
            counts["done"] += 1
        elif st == "running":
            counts["running"] += 1
            running_nodes.append(f"{name}@{worker}" if worker else name)
        elif st == "ready":
            counts["ready"] += 1
        elif st == "failed":
            counts["failed"] += 1
            failed_nodes.append(name)
        elif st == "blocked":
            counts["blocked"] += 1
            blocked_nodes.append(name)
        elif st == "skipped":
            counts["skipped"] += 1
        else:
            counts["pending"] += 1

    summary_line = (
        f"📊 [Nœuds] done={counts['done']}, running={counts['running']}, "
        f"ready={counts['ready']}, failed={counts['failed']}, blocked={counts['blocked']}"
    )
    if counts["skipped"]:
        summary_line += f", skipped={counts['skipped']}"
    if counts["pending"]:
        summary_line += f", pending={counts['pending']}"

    details = []
    if running_nodes:
        details.append(f"▶️  En cours: {', '.join(running_nodes)}")
    if failed_nodes:
        details.append(f"❌ Échoués: {', '.join(failed_nodes)}")
    if blocked_nodes:
        details.append(f"⛔ Bloqués: {', '.join(blocked_nodes)}")

    return summary_line, details


def print_final_dag_summary(nodes, job_id):
    """Affiche le récapitulatif complet de tous les nœuds à la fin du job."""
    if not nodes:
        return
    summary_line, details = format_nodes_status_summary(nodes)
    if not summary_line:
        return
    print("\n" + "=" * 60)
    print(f"📊 RÉSUMÉ D'EXÉCUTION DES NŒUDS (Job: {job_id})")
    print(f"   {summary_line}")
    if details:
        for d in details:
            print(f"   {d}")
    print("=" * 60)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

def get_ram_requirement(repo=None, branch=None, is_local=False, local_repo_path=None):
    """
    Reads RAM requirement from the .cluster-ci file.
    First tries to fetch the file locally if is_local=True, or from the remote repo (shallow clone),
    then falls back to reading from the current working directory.
    Expected format in .cluster-ci: --ram 16 or REQUIRED_RAM=16GB
    """
    content = None

    if is_local and local_repo_path:
        ci_file = os.path.join(local_repo_path, ".cluster-ci")
        if os.path.exists(ci_file):
            try:
                with open(ci_file, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
            except Exception:
                pass
        else:
            return DEFAULT_RAM_GB

    # Strategy 1: Fetch .cluster-ci from the remote repo
    if content is None and repo and branch and not is_local:
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
            subprocess.run(["git", "checkout", "HEAD", "--", ".cluster-ci"],
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
            return DEFAULT_RAM_GB

    import re
    # Try REQUIRED_RAM=16GB or REQUIRED_RAM=16.5
    match_env = re.search(r'REQUIRED_RAM\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    if match_env:
        return float(match_env.group(1))

    # Try --ram 16
    match = re.search(r'--ram\s+(\d+(?:\.\d+)?)', content)
    if match:
        return float(match.group(1))
    return DEFAULT_RAM_GB

def get_config_value(pattern, content, default=None, is_float=False):
    import re
    match = re.search(pattern, content)
    if match:
        val = match.group(1)
        return float(val) if is_float else val
    return default

def submit_job(headnode_url, repo, branch, gh_token=None, env_vars=None, commit_hash=None, is_local=False, local_repo_path=None):
    """Submits a research job to the headnode scheduler."""
    if not headnode_url:
        print("Error: HEADNODE_URL is required to submit a job.")
        sys.exit(1)

    # Active JIT Network Diagnostic
    try:
        print(f"Connecting to headnode at {headnode_url} (checking connectivity)...")
        requests.get(f"{headnode_url}/check_space", timeout=3)
    except requests.exceptions.Timeout:
        print(f"Error: Connection to headnode at {headnode_url} timed out (limit: 3s).")
        print("   Please check that the headnode service is running and accessible.")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Error: Could not connect to headnode at {headnode_url}: {e}")
        print("   Please verify the URL and network configuration.")
        sys.exit(1)
    if not commit_hash:
        commit_hash = os.environ.get("CALLER_COMMIT_SHA") or os.environ.get("GITHUB_SHA")
        if not commit_hash:
            try:
                import subprocess
                commit_hash = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
            except Exception:
                commit_hash = "local-head"
    # Strategy: Fetch .cluster-ci content first to parse all requirements
    content = None
    if is_local and local_repo_path:
        ci_file = os.path.join(local_repo_path, ".cluster-ci")
        if os.path.exists(ci_file):
            try:
                with open(ci_file, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
            except Exception as e:
                print(f"⚠️ Could not read local .cluster-ci from {ci_file}: {e}")

    if content is None and not is_local:
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
            subprocess.run(["git", "checkout", "HEAD", "--", ".cluster-ci"],
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
    ram_req = DEFAULT_RAM_GB
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

    # Parse REQUIRED_VRAM
    vram_req = 0
    vram_match = re.search(r'REQUIRED_VRAM\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    if vram_match:
        vram_req = float(vram_match.group(1))

    # Parse CUSTOM_WEB_APP
    custom_web_app = False
    custom_app_match = re.search(r'CUSTOM_WEB_APP\s*=\s*(true|1)', content, re.IGNORECASE)
    if custom_app_match:
        custom_web_app = True

    # Parse ALLOWED_WORKERS (comma-separated hostnames to restrict execution)
    allowed_workers = None
    aw_match = re.search(r'ALLOWED_WORKERS\s*=\s*(.+)', content)
    if aw_match:
        allowed_workers = [h.strip() for h in aw_match.group(1).split(',') if h.strip()]

    # Parse REQUIRED_CPUS (A16)
    cpus_match = re.search(r'REQUIRED_CPUS\s*=\s*(\d+)', content)
    cpus_req = int(cpus_match.group(1)) if cpus_match else None

    # Parse REQUIRED_GPUS (A16)
    gpus_match = re.search(r'REQUIRED_GPUS\s*=\s*(\d+)', content)
    if gpus_match:
        gpus_req = int(gpus_match.group(1))
    elif vram_req > 0:
        gpus_req = 1
    else:
        gpus_req = None

    # Parse REQUIRED_STORAGE or REQUIRED_DISK (A16)
    storage_match = re.search(r'(?:REQUIRED_STORAGE|REQUIRED_DISK)\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    storage_req = float(storage_match.group(1)) if storage_match else None

    # Parse PARALLEL_STAGES & execution planificateur W1 (v3)
    parallel_stages_match = re.search(r'^\s*PARALLEL_STAGES\s*=\s*(true|1)\b', content, re.IGNORECASE | re.MULTILINE)
    parallel_stages_enabled = bool(parallel_stages_match)

    target_repo_dir = local_repo_path if (is_local and local_repo_path) else os.path.abspath(os.getcwd())
    dvc_yaml_exists = os.path.isfile(os.path.join(target_repo_dir, "dvc.yaml"))

    plan = None
    if parallel_stages_enabled and dvc_yaml_exists:
        print(f"🧩 PARALLEL_STAGES enabled and dvc.yaml found: generating v3 plan via W1 planner...")
        plan = run_planner_for_submission(target_repo_dir)
        print(f"✅ Planner generated plan successfully ({len(plan.get('nodes', []))} node(s)).")
    elif parallel_stages_enabled and not dvc_yaml_exists:
        print(f"⚠️ PARALLEL_STAGES=true requested but dvc.yaml not found in {target_repo_dir}. Submitting without plan.")

    submit_info = f"🚀 Submitting job for {repo}@{branch} (RAM: {ram_req}GB, VRAM: {vram_req}GB, Timeout: {max_runtime}h, Custom App: {custom_web_app})"
    if is_local:
        submit_info += f" [Local Mode: {local_repo_path}]"
    if allowed_workers:
        submit_info += f" [Allowed Workers: {', '.join(allowed_workers)}]"
    if plan is not None:
        submit_info += f" [v3 Parallel Plan: {len(plan.get('nodes', []))} node(s)]"
    print(submit_info)

    token = os.environ.get("CLUSTER_TOKEN")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        print(f"Connecting to headnode at {headnode_url}...")
        payload = {
            "repo": repo,
            "branch": branch,
            "commit_hash": commit_hash,
            "ram_required_gb": ram_req,
            "vram_required_gb": vram_req,
            "max_runtime_hours": max_runtime,
            "exposed_port": exposed_port,
            "custom_web_app": custom_web_app,
            "allowed_workers": allowed_workers,
            "gh_run_id": os.environ.get("GITHUB_RUN_ID"),
            "gh_token": gh_token,
            "env_vars": env_vars,
            "username": os.environ.get("GITHUB_ACTOR", "unknown"),
            "is_local": is_local,
            "local_repo_path": local_repo_path,
        }
        if cpus_req is not None:
            payload["cpus"] = cpus_req
            payload["required_cpus"] = cpus_req
        if gpus_req is not None:
            payload["gpus"] = gpus_req
            payload["required_gpus"] = gpus_req
        if storage_req is not None:
            payload["storage_gb"] = storage_req
            payload["required_storage"] = storage_req
        if plan is not None:
            payload["plan"] = plan

        resp = requests.post(f"{headnode_url}/submit_job", json=payload, headers=headers, timeout=10)
        if resp.status_code >= 400:
            err_msg = resp.text
            try:
                err_json = resp.json()
                if isinstance(err_json, dict) and "error" in err_json:
                    err_msg = err_json["error"]
            except Exception:
                pass
            print(f"❌ Error submitting job (HTTP {resp.status_code}): {err_msg}", file=sys.stderr)
            sys.exit(1)
        resp.raise_for_status()
        job_data = resp.json()
        job_id = job_data['job_id']
        print(f"✅ Job submitted successfully! ID: {job_id}")

        # Immediately detach gh_run_id from the job in headnode DB.
        # Why: The ephemeral GHA runner exits shortly after submitting the job,
        # which marks the GHA run as "completed/failure". The periodic clean_ghosts
        # task would then detect this as a ghost job and kill the still-running
        # worker container. By clearing gh_run_id, we make the job invisible to
        # clean_ghosts. Real cancellations are still handled by the SIGTERM signal
        # handler in wait_for_job(), which contacts the worker directly via /cancel/.
        try:
            requests.post(f"{headnode_url}/update_job_status", json={
                "job_id": job_id,
                "detach_gha": True
            }, headers=headers, timeout=5)
        except Exception:
            pass  # Best-effort; failure here is non-critical

        return job_id
    except Exception as e:
        print(f"❌ Failed to submit job: {e}")
        sys.exit(1)

def wait_for_job(headnode_url, job_id, branch=None):
    """Polls the headnode for job status and streams logs from the worker."""
    if not headnode_url:
        print("Error: HEADNODE_URL is required to check job status.")
        sys.exit(1)
    print(f"⏳ Waiting for job {job_id} to complete...")

    is_draft_branch = branch and branch.startswith("cluster-draft/")

    def signal_handler(sig, frame):
        token = os.environ.get("CLUSTER_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        # === DRAFT BRANCHES OU INTERRUPT UTILISATEUR (SIGINT / Ctrl+C) : Annulation du job entier ===
        if is_draft_branch or sig == signal.SIGINT:
            worker_url = None
            cancel_error = None

            # 1. Annulation globale sur le Headnode via POST /api/jobs/{job_id}/stop (A17 / Multi-machines)
            try:
                stop_resp = requests.post(f"{headnode_url}/api/jobs/{job_id}/stop", headers=headers, timeout=10)
                if stop_resp.status_code not in (200, 404):
                    cancel_error = f"Headnode returned HTTP {stop_resp.status_code}: {stop_resp.text}"
            except Exception as e:
                cancel_error = e

            # 2. Récupérer l'URL du worker si disponible pour notification de repli direct
            try:
                resp = requests.get(f"{headnode_url}/job_status/{job_id}", timeout=10)
                if resp.status_code == 200:
                    job = resp.json()
                    worker_url = job.get('worker_service_url')
            except Exception as e:
                if not cancel_error:
                    cancel_error = e

            if worker_url:
                try:
                    requests.post(f"{worker_url}/cancel/{job_id}", timeout=10)
                except Exception as e:
                    if not cancel_error:
                        cancel_error = e

            try:
                requests.post(f"{headnode_url}/update_job_status", json={
                    "job_id": job_id,
                    "status": "failed",
                    "exit_code": -signal.SIGTERM
                }, headers=headers, timeout=10)
            except Exception as e:
                if not cancel_error:
                    cancel_error = e

            # Messages de journalisation enveloppés
            try:
                print(f"\n🛑 Signal received ({signal.Signals(sig).name}). Propagating full cancellation to headnode...")
                if cancel_error:
                    print(f"⚠️ Error during cancellation: {cancel_error}")
                else:
                    print("✅ Cancellation signal sent.")
            except (BrokenPipeError, Exception):
                pass

            sys.exit(128 + sig)

        # === NON-DRAFT BRANCHES (SIGTERM GHA) : Detach GHA without killing worker job ===
        else:
            try:
                requests.post(f"{headnode_url}/update_job_status", json={
                    "job_id": job_id,
                    "detach_gha": True
                }, headers=headers, timeout=10)
            except Exception:
                pass
            try:
                print(f"\n🔄 GHA workflow replaced (branch: {branch}). Job {job_id} continues running on worker.")
            except (BrokenPipeError, Exception):
                pass
            sys.exit(128 + sig)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log_offset = 0
    status_printed = False
    oom_detected = False
    last_queue_check = 0
    last_status = None
    last_queue_diagnostic = None
    last_nodes_summary = None

    while True:
        try:
            resp = requests.get(f"{headnode_url}/job_status/{job_id}", timeout=10)
            resp.raise_for_status()
            job = resp.json()
            status = job['status']
            worker_url = job.get('worker_service_url')
            ram_required = job.get('ram_required_gb', DEFAULT_RAM_GB)

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
                            
                            # Build diagnostic lines
                            diag_lines = []
                            diag_lines.append("═"*55)
                            diag_lines.append(f"⏳ FILE D'ATTENTE CLUSTER-CI (Job: {job_id[:8]})")
                            if own_position != -1:
                                diag_lines.append(f"   👉 Position dans la file : {own_position} / {len(queue)}")
                            else:
                                diag_lines.append(f"   👉 Position dans la file : En cours d'analyse par le scheduler...")
                            
                            # Diagnostic if RAM required exceeds maximum physical capacity in the cluster
                            if online_workers and ram_required > (max_ram - 2.0):
                                diag_lines.append(f"   ⚠️  CRITIQUE : Votre tâche demande {ram_required:.1f} GB de RAM.")
                                diag_lines.append(f"      Mais la capacité maximale des machines en ligne (moins 2GB de marge OS) est de {max_ram - 2.0:.1f} GB.")
                                diag_lines.append(f"      Ce job ne pourra JAMAIS démarrer ! Veuillez baisser REQUIRED_RAM dans .cluster-ci.")
                            elif online_workers and not compatible_workers:
                                diag_lines.append(f"   ⚠️  ATTENTE : Aucune machine actuellement en ligne ne dispose d'assez de RAM physique ({ram_required:.1f} GB requis).")
                                diag_lines.append(f"      En attente qu'un worker avec une capacité suffisante vienne s'enregistrer.")
                            elif online_workers and compatible_workers:
                                # Check if all compatible workers are busy
                                all_busy = all([w.get("active_job") is not None for w in compatible_workers])
                                if all_busy:
                                    diag_lines.append(f"   ⚠️  ATTENTE : Toutes les machines compatibles avec vos besoins en RAM ({ram_required:.1f} GB) sont occupées.")
                                    
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
                                            diag_lines.append(f"      👉 Temps d'attente maximum estimé : ~{time_str} (dès que le premier worker compatible se libère)")
                                        else:
                                            diag_lines.append(f"      👉 Temps d'attente : Estimé après libération et traitement de {own_position - 1} job(s) devant vous.")
                            
                            # Current running jobs details on each machine
                            diag_lines.append("   🖥️  Statut des machines du cluster :")
                            if not online_workers:
                                diag_lines.append("      ❌ Aucune machine n'est actuellement en ligne ou active.")
                            else:
                                for w in online_workers:
                                    active_job = w.get("active_job")
                                    is_compatible = (w["total_ram_gb"] - 2.0) >= ram_required
                                    worker_vram = w.get('total_vram_gb', 0)
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
                                                
                                        diag_lines.append(f"      ● {w['hostname']} : OCCUPÉE par {active_job['username']} [{active_job['repo'].split('/')[-1]}] ({duration_str}, reste {remaining_str}) [{w['total_ram_gb']:.0f}GB RAM, {worker_vram:.0f}GB VRAM]")
                                    else:
                                        diag_lines.append(f"      ○ {w['hostname']} : LIBRE ({w['total_ram_gb']:.0f}GB RAM, {w.get('total_vram_gb', 0):.0f}GB VRAM)")
                                        
                            # Waiting queue list
                            if len(queue) > 1:
                                diag_lines.append("   📋 Jobs en attente devant vous :")
                                count = 0
                                for q_job in queue:
                                    if q_job["job_id"] == job_id:
                                        break
                                    count += 1
                                    if count <= 3:
                                        diag_lines.append(f"      #{count} : Job [{q_job['repo'].split('/')[-1]}] par [{q_job['username']}] (demande {q_job['ram_required_gb']:.1f} GB)")
                                if len(queue) - 1 > count:
                                    diag_lines.append(f"      ... et {len(queue) - 1 - count} autre(s) job(s)")
                                    
                            diag_lines.append("═"*55)
                            
                            diag_str = "\n".join(diag_lines) + "\n"
                            if diag_str != last_queue_diagnostic:
                                sys.stdout.write(diag_str)
                                sys.stdout.flush()
                                last_queue_diagnostic = diag_str
                    except Exception as e:
                        # Fallback silently to prevent blocking the execution loop
                        pass

            # Suivi de l'état des nœuds DAG (v3 multi-nœuds)
            nodes_data = job.get('nodes') or job.get('job_nodes')
            if nodes_data:
                summary_line, details = format_nodes_status_summary(nodes_data)
                if summary_line and summary_line != last_nodes_summary:
                    sys.stdout.write(f"\n{summary_line}\n")
                    for d in details:
                        sys.stdout.write(f"   {d}\n")
                    sys.stdout.flush()
                    last_nodes_summary = summary_line

            # Récupération des logs : Headnode agrégé en priorité (v3 [nœud@machine]), fallback worker_url
            logs_resp = None
            if headnode_url:
                try:
                    h_resp = requests.get(f"{headnode_url}/job_logs/{job_id}?offset={log_offset}", timeout=5)
                    if h_resp.status_code == 200:
                        logs_resp = h_resp
                    elif h_resp.status_code == 404:
                        h_resp2 = requests.get(f"{headnode_url}/api/jobs/{job_id}/logs?offset={log_offset}", timeout=5)
                        if h_resp2.status_code == 200:
                            logs_resp = h_resp2
                except requests.exceptions.RequestException:
                    pass  # Tolérer les micro-coupures réseau transitoires lors du polling
                except Exception as unexpected_err:
                    sys.stderr.write(f"\n⚠️ Erreur inattendue polling logs headnode: {unexpected_err}\n")

            if logs_resp is None and worker_url:
                try:
                    logs_resp = requests.get(f"{worker_url}/job_logs/{job_id}?offset={log_offset}", timeout=5)
                except requests.exceptions.RequestException:
                    pass  # Tolérer les micro-coupures réseau transitoires lors du polling
                except Exception as unexpected_err:
                    sys.stderr.write(f"\n⚠️ Erreur inattendue polling logs worker: {unexpected_err}\n")

            if logs_resp and logs_resp.status_code == 200:
                try:
                    logs_data = logs_resp.json()
                    new_logs = logs_data.get('logs', '')
                    if new_logs:
                        import re
                        if re.search(r'tué par le système \(OOM Killer\)|arrêté préventivement par le GPU Watchdog|Exit code 137|Out of Memory|exited with -9', new_logs, re.IGNORECASE):
                            oom_detected = True
                        if not status_printed:
                            print(f"\n\n[Streaming logs for job {job_id}]")
                            status_printed = True
                        sys.stdout.write(new_logs)
                        sys.stdout.flush()
                        log_offset = logs_data.get('offset', log_offset)
                except (ValueError, KeyError) as json_err:
                    sys.stderr.write(f"\n⚠️ Format de logs invalide reçu du headnode/worker: {json_err}\n")
                except Exception as unexpected_err:
                    sys.stderr.write(f"\n⚠️ Erreur inattendue traitement logs: {unexpected_err}\n")

            if status == 'completed':
                print_final_dag_summary(nodes_data, job_id)
                print(f"\n✅ Job {job_id} completed successfully!")
                return 0
            elif status == 'failed':
                print_final_dag_summary(nodes_data, job_id)
                exit_code = job.get('exit_code')
                if exit_code is None or exit_code == 0:
                    exit_code = 1  # Ensure non-zero exit on failure

                # A17 : Remonter fidèlement le message d'erreur du headnode ou des nœuds
                job_error = job.get('error_message') or job.get('error')
                if job_error:
                    print(f"\n❌ Error message: {job_error}")
                if nodes_data and isinstance(nodes_data, list):
                    for nd in nodes_data:
                        if isinstance(nd, dict) and nd.get('status') == 'failed' and nd.get('error_message'):
                            print(f"❌ Node '{nd.get('name')}' failed: {nd.get('error_message')}")

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
                elif exit_code == 255:
                    print(f"\n❌ Critical Failure: Job {job_id} execution process aborted unexpectedly (Exit code 255).")
                else:
                    if not job_error:
                        print(f"\n❌ Job {job_id} failed with exit code {exit_code}")
                return exit_code

            if not status_printed and status != last_status:
                sys.stdout.write(f"Status: {status}...\n")
                sys.stdout.flush()
                last_status = status

        except Exception as e:
            print(f"\n⚠️ Error checking status: {e}")

        time.sleep(2)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Submit a job to Cluster-CI Scheduler")
    parser.add_argument("repo", help="Target repository (owner/repo)")
    parser.add_argument("branch", help="Target branch")
    parser.add_argument("--headnode", default=os.environ.get("HEADNODE_URL"), help="Headnode URL")
    parser.add_argument("--gh-token", default=None, help="GitHub token for cloning private repos")
    parser.add_argument("--local", action="store_true", help="Submit local directory without git clone")
    parser.add_argument("--local-repo-path", default=None, help="Path to local repository")
    parser.add_argument("-e", "--env", action="append", default=[], help="Environment variables (KEY=VAL)")

    args = parser.parse_args()

    if not args.headnode:
        print("Error: HEADNODE_URL environment variable is missing and no --headnode argument was provided.")
        print("   Please set the HEADNODE_URL environment variable or provide the --headnode parameter.")
        sys.exit(1)

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

    local_repo_path = args.local_repo_path or (os.path.abspath(os.getcwd()) if args.local else None)
    job_id = submit_job(
        args.headnode, args.repo, args.branch, args.gh_token, env_vars,
        is_local=args.local, local_repo_path=local_repo_path
    )
    exit_code = wait_for_job(args.headnode, job_id, branch=args.branch)
    sys.exit(exit_code)
