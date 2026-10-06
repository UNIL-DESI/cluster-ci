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
    """Retourne le module du planificateur W1 (src.planner.stage_plan).

    Aucun repli vers un autre module : erreur explicite si introuvable.
    """
    env_mod = os.environ.get("CLUSTER_CI_PLANNER_MODULE")
    if env_mod:
        return env_mod
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
            "--no-project",
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

    cluster_ci_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    existing_paths = [p for p in pythonpath.split(os.pathsep) if p]
    all_paths = [cluster_ci_root, target_repo] + [p for p in existing_paths if p not in (cluster_ci_root, target_repo)]
    env["PYTHONPATH"] = os.pathsep.join(all_paths)

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
        print(f"❌ Error: Unable to execute planner ({' '.join(cmd)}): {e}", file=sys.stderr)
        sys.exit(1)

    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or "").strip()
        print(
            f"❌ Error: Planner failed (code {proc.returncode}):\n{err_msg}",
            file=sys.stderr,
        )
        sys.exit(proc.returncode if proc.returncode != 0 else 1)

    output = proc.stdout.strip()
    try:
        plan_data = json.loads(output)
        return plan_data
    except json.JSONDecodeError as e:
        print(
            f"❌ Error: Invalid JSON output from planner: {e}\nRaw output:\n{output}",
            file=sys.stderr,
        )
        sys.exit(1)


def check_repo_remote_matches(repo_dir, expected_repo):
    """Vérifie que le remote origin du dépôt cible correspond au dépôt soumis.

    Retourne (matches, actual_remote, error_detail).
    Si aucun remote origin n'est configuré (ex. dépôt de test local sans origin), retourne (True, None, None).
    """
    import subprocess
    import re

    try:
        res = subprocess.run(
            ["git", "-C", repo_dir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return True, None, None
        actual_remote = res.stdout.strip()
        if not actual_remote:
            return True, None, None

        def _normalize(url):
            u = re.sub(r"^(?:https?://|git@|ssh://|git://)[^/:]+[/:]", "", url)
            u = u.rstrip("/").removesuffix(".git")
            return u.lower()

        clean_remote = _normalize(actual_remote)
        clean_exp = _normalize(expected_repo)

        if clean_remote == clean_exp or clean_remote.endswith("/" + clean_exp) or clean_exp.endswith("/" + clean_remote):
            return True, actual_remote, None

        remote_base = clean_remote.split("/")[-1]
        exp_base = clean_exp.split("/")[-1]
        if remote_base == exp_base and ("/" not in clean_exp or "/" not in clean_remote):
            return True, actual_remote, None

        return False, actual_remote, f"remote origin '{clean_remote}' != repo attendu '{clean_exp}'"
    except Exception:
        return True, None, None


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
        "missing_deps": 0,
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
        elif st in ("missing_deps", "missing-deps"):
            counts["missing_deps"] += 1
            blocked_nodes.append(f"{name} (missing_deps)")
        elif st == "skipped":
            counts["skipped"] += 1
        else:
            counts["pending"] += 1

    summary_line = (
        f"📊 [Nodes] done={counts['done']}, running={counts['running']}, "
        f"ready={counts['ready']}, failed={counts['failed']}, blocked={counts['blocked']}"
    )
    if counts["missing_deps"]:
        summary_line += f", missing_deps={counts['missing_deps']}"
    if counts["skipped"]:
        summary_line += f", skipped={counts['skipped']}"
    if counts["pending"]:
        summary_line += f", pending={counts['pending']}"

    details = []
    if running_nodes:
        details.append(f"▶️  Running: {', '.join(running_nodes)}")
    if failed_nodes:
        details.append(f"❌ Failed: {', '.join(failed_nodes)}")
    if blocked_nodes:
        details.append(f"⛔ Blocked: {', '.join(blocked_nodes)}")

    return summary_line, details


def print_final_dag_summary(nodes, job_id):
    """Affiche le récapitulatif complet de tous les nœuds à la fin du job."""
    if not nodes:
        return
    summary_line, details = format_nodes_status_summary(nodes)
    if not summary_line:
        return
    print("\n" + "=" * 60)
    print(f"📊 NODE EXECUTION SUMMARY (Job: {job_id})")
    print(f"   {summary_line}")
    if details:
        for d in details:
            print(f"   {d}")
    print("=" * 60)


def format_multi_machine_log_line(line, nodes_data=None):
    """Garantit que chaque ligne est préfixée [nœud@machine] lorsque plusieurs nœuds tournent en parallèle."""
    import re
    if not line or not line.strip():
        return line

    stripped = line.strip()
    if not nodes_data or not isinstance(nodes_data, list):
        return line

    active_nodes = [
        n for n in nodes_data
        if isinstance(n, dict) and n.get("status") in ("running", "assigned")
    ]
    candidates = active_nodes if active_nodes else [n for n in nodes_data if isinstance(n, dict)]

    # 1. La ligne commence par un préfixe entre crochets [tag]
    m_bracket = re.match(r"^\[([^\]]+)\]\s*(.*)$", stripped)
    if m_bracket:
        tag = m_bracket.group(1).strip()
        rest = m_bracket.group(2)
        for nd in candidates:
            n_name = nd.get("name") or nd.get("node_name")
            machine = nd.get("machine") or nd.get("worker_id") or "worker"
            if not n_name:
                continue
            if tag == f"{n_name}@{machine}":
                return line  # Déjà préfixé avec nœud et machine
            if tag == n_name:
                return f"[{n_name}@{machine}] {rest}"
        # Si le tag se termine déjà par une machine connue
        machines_all = {
            nd.get("machine") or nd.get("worker_id")
            for nd in candidates
            if (nd.get("machine") or nd.get("worker_id"))
        }
        for m in machines_all:
            if tag.endswith(f"@{m}"):
                return line
        return line

    # 2. La ligne commence par "node_name: ..."
    for nd in candidates:
        n_name = nd.get("name") or nd.get("node_name")
        if n_name and stripped.startswith(f"{n_name}:"):
            rest = stripped[len(n_name) + 1:].lstrip()
            machine = nd.get("machine") or nd.get("worker_id") or "worker"
            return f"[{n_name}@{machine}] {rest}"

    # 3. Plusieurs nœuds tournent en parallèle sur des machines distinctes
    machines = {
        nd.get("machine") or nd.get("worker_id")
        for nd in active_nodes
        if (nd.get("machine") or nd.get("worker_id"))
    }
    if len(active_nodes) > 1 and len(machines) > 1:
        for nd in active_nodes:
            n_name = nd.get("name") or nd.get("node_name")
            if n_name and n_name in stripped:
                machine = nd.get("machine") or nd.get("worker_id") or "worker"
                return f"[{n_name}@{machine}] {stripped}"
        first_node = active_nodes[0].get("name") or "parallel"
        first_mach = active_nodes[0].get("machine") or active_nodes[0].get("worker_id") or "cluster"
        return f"[{first_node}@{first_mach}] {stripped}"

    return line


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

def submit_job(headnode_url, repo, branch, gh_token=None, env_vars=None, commit_hash=None, is_local=False, local_repo_path=None, repo_dir=None):
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
    target_repo_dir = repo_dir or (local_repo_path if (is_local and local_repo_path) else None)
    if not target_repo_dir and os.environ.get("GITHUB_WORKSPACE"):
        target_repo_dir = os.environ.get("GITHUB_WORKSPACE")

    if target_repo_dir:
        ci_file = os.path.join(target_repo_dir, ".cluster-ci")
        if os.path.exists(ci_file):
            try:
                with open(ci_file, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
            except Exception as e:
                print(f"⚠️ Could not read .cluster-ci from {ci_file}: {e}")

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

    if content is None and target_repo_dir and os.path.exists(os.path.join(target_repo_dir, ".cluster-ci")):
        with open(os.path.join(target_repo_dir, ".cluster-ci"), 'r') as f:
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

    plan = None
    if parallel_stages_enabled:
        # A17 / Fail-Fast : Interdiction stricte de construire le plan depuis le CWD par défaut
        if not target_repo_dir:
            print(
                f"❌ [A17] Target repository validation error for v3 planning:\n"
                f"No target repository directory specified for '{repo}'.\n"
                f"Cause: PARALLEL_STAGES=true requires building the plan from the target repository's dvc.yaml, "
                f"but no path was provided (--repo-dir or --local-repo-path) and defaulting to CWD is strictly forbidden.\n"
                f"Remedy: Specify the target repository path via --repo-dir or --local-repo-path.",
                file=sys.stderr,
            )
            sys.exit(1)

        target_repo_dir = os.path.abspath(target_repo_dir)
        if not os.path.isdir(target_repo_dir):
            print(
                f"❌ [A17] Target repository validation error for v3 planning:\n"
                f"Target directory '{target_repo_dir}' not found.\n"
                f"Cause: Repository for '{repo}' has not been checked out or path is invalid.\n"
                f"Remedy: Ensure target repository is checked out locally and pass its path via --repo-dir.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Validation du remote origin (git -C <dir> remote get-url origin contre repo)
        matches, actual_remote, error_detail = check_repo_remote_matches(target_repo_dir, repo)
        if not matches:
            print(
                f"❌ [A17] Target repository validation error for v3 planning:\n"
                f"Directory '{target_repo_dir}' has remote origin '{actual_remote}', "
                f"which does not match submitted repository '{repo}'.\n"
                f"Cause: Mismatch between local directory and requested target repository ({error_detail}).\n"
                f"Remedy: Provide path to repository actually checked out for '{repo}' via --repo-dir, or clone the correct repository.",
                file=sys.stderr,
            )
            sys.exit(1)

        dvc_yaml_path = os.path.join(target_repo_dir, "dvc.yaml")
        if not os.path.isfile(dvc_yaml_path):
            print(
                f"❌ [A17] DVC pipeline validation error for v3 planning:\n"
                f"File dvc.yaml not found in '{target_repo_dir}'.\n"
                f"Cause: PARALLEL_STAGES=true is enabled but target repository '{repo}' does not contain dvc.yaml.\n"
                f"Remedy: Create a dvc.yaml file defining pipeline stages or disable PARALLEL_STAGES in .cluster-ci.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"🧩 PARALLEL_STAGES enabled and dvc.yaml found: generating v3 plan via W1 planner for {repo}...")
        plan = run_planner_for_submission(target_repo_dir)
        print(f"✅ Planner generated plan successfully ({len(plan.get('nodes', []))} node(s)).")

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


class NetworkRetryTracker:
    """Suivi et bornage unifié des tentatives réseau et d'indisponibilité (15 min max, backoff <= 60s)."""

    def __init__(
        self,
        max_retries=10,
        max_downtime_seconds=900.0,
        base_backoff_seconds=2.0,
        max_backoff_seconds=60.0,
    ):
        self.max_retries = max_retries
        self.max_downtime_seconds = max_downtime_seconds
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.consecutive_errors = 0
        self.first_error_time = None

    def record_success(self):
        """Réinitialise les compteurs lors d'une requête réussie."""
        self.consecutive_errors = 0
        self.first_error_time = None

    def get_sleep_interval(self):
        """Calcule le délai d'attente avec backoff exponentiel plafonné à 60s."""
        if self.consecutive_errors <= 0:
            return self.base_backoff_seconds
        backoff = self.base_backoff_seconds * (2 ** (self.consecutive_errors - 1))
        return min(self.max_backoff_seconds, backoff)

    @property
    def is_exhausted(self):
        """Vérifie si le seuil de retries ou de temps de panne max est atteint."""
        if self.consecutive_errors >= self.max_retries:
            return True
        if self.first_error_time is not None:
            downtime = time.monotonic() - self.first_error_time
            if downtime >= self.max_downtime_seconds:
                return True
        return False

    def record_error(self, error, endpoint_desc, target_url, job_id=None):
        """Incrémente le compteur d'erreurs et vérifie les bornes.

        Retourne (is_exhausted, message).
        """
        self.consecutive_errors += 1
        now = time.monotonic()
        if self.first_error_time is None:
            self.first_error_time = now
        downtime = now - self.first_error_time

        is_exhausted = self.is_exhausted

        attach_cmd = (
            f"cluster-run attach {job_id}  (or: python -m src.scheduler.submit_job --attach {job_id} --headnode {target_url})"
            if job_id
            else "cluster-run view"
        )

        if is_exhausted:
            msg = (
                f"\n❌ [Network] Exceeded maximum network retries ({self.consecutive_errors}/{self.max_retries}) "
                f"or downtime ({downtime:.1f}s/{self.max_downtime_seconds}s) while polling {endpoint_desc}.\n"
                f"Cause: {endpoint_desc} at {target_url} unreachable: {error}\n"
                f"Status: Job {job_id or ''} CONTINUES RUNNING on the cluster (not canceled).\n"
                f"Remedy: Check headnode/network connectivity, then re-attach with:\n"
                f"   {attach_cmd}\n"
            )
        else:
            sleep_int = self.get_sleep_interval()
            msg = (
                f"\n⚠️ [Network] Temporary failure polling {endpoint_desc}: {error} "
                f"(attempt {self.consecutive_errors}/{self.max_retries}, downtime {downtime:.1f}s, backoff {sleep_int:.0f}s)\n"
            )

        return is_exhausted, msg


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
    fallback_warned = False
    last_queue_check = 0
    last_status = None
    last_queue_diagnostic = None
    last_nodes_summary = None
    log_retry_tracker = NetworkRetryTracker(max_retries=10, max_downtime_seconds=900.0)
    status_retry_tracker = NetworkRetryTracker(max_retries=10, max_downtime_seconds=900.0)

    while True:
        try:
            resp = requests.get(f"{headnode_url}/job_status/{job_id}", timeout=10)
            resp.raise_for_status()
            status_retry_tracker.record_success()
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
                                diag_lines.append(f"   👉 Queue position: {own_position} / {len(queue)}")
                            else:
                                diag_lines.append(f"   👉 Queue position: Analyzing by scheduler...")
                            
                            # Diagnostic if RAM required exceeds maximum physical capacity in the cluster
                            if online_workers and ram_required > (max_ram - 2.0):
                                diag_lines.append(f"   ⚠️  CRITICAL: Your task requests {ram_required:.1f} GB of RAM.")
                                diag_lines.append(f"      However, the maximum capacity of online machines (minus 2GB OS headroom) is {max_ram - 2.0:.1f} GB.")
                                diag_lines.append(f"      This job will NEVER be able to start! Please decrease REQUIRED_RAM in .cluster-ci.")
                            elif online_workers and not compatible_workers:
                                diag_lines.append(f"   ⚠️  WAITING: No online machine currently has enough physical RAM ({ram_required:.1f} GB required).")
                                diag_lines.append(f"      Waiting for a worker with sufficient capacity to register.")
                            elif online_workers and compatible_workers:
                                # Check if all compatible workers are busy
                                all_busy = all([w.get("active_job") is not None for w in compatible_workers])
                                if all_busy:
                                    diag_lines.append(f"   ⚠️  WAITING: All machines compatible with your RAM requirements ({ram_required:.1f} GB) are busy.")
                                    
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
                                            diag_lines.append(f"      👉 Maximum estimated wait time: ~{time_str} (as soon as the first compatible worker becomes free)")
                                        else:
                                            diag_lines.append(f"      👉 Estimated wait time: Calculated after freeing and processing {own_position - 1} job(s) ahead of you.")
                            
                            # Current running jobs details on each machine
                            diag_lines.append("   🖥️  Cluster machine status:")
                            if not online_workers:
                                diag_lines.append("      ❌ No machine is currently online or active.")
                            else:
                                for w in online_workers:
                                    active_job = w.get("active_job")
                                    is_compatible = (w["total_ram_gb"] - 2.0) >= ram_required
                                    worker_vram = w.get('total_vram_gb', 0)
                                    comp_str = "Compatible" if is_compatible else "Insufficient RAM"
                                    
                                    if active_job:
                                        duration_str = "running"
                                        remaining_str = "undetermined"
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
                                                
                                        diag_lines.append(f"      ● {w['hostname']} : BUSY with {active_job['username']} [{active_job['repo'].split('/')[-1]}] ({duration_str}, remaining {remaining_str}) [{w['total_ram_gb']:.0f}GB RAM, {worker_vram:.0f}GB VRAM]")
                                    else:
                                        diag_lines.append(f"      ○ {w['hostname']} : IDLE ({w['total_ram_gb']:.0f}GB RAM, {w.get('total_vram_gb', 0):.0f}GB VRAM)")
                                        
                            # Waiting queue list
                            if len(queue) > 1:
                                diag_lines.append("   📋 Waiting jobs ahead of you:")
                                count = 0
                                for q_job in queue:
                                    if q_job["job_id"] == job_id:
                                        break
                                    count += 1
                                    if count <= 3:
                                        diag_lines.append(f"      #{count} : Job [{q_job['repo'].split('/')[-1]}] by [{q_job['username']}] (requests {q_job['ram_required_gb']:.1f} GB)")
                                if len(queue) - 1 > count:
                                    diag_lines.append(f"      ... and {len(queue) - 1 - count} other job(s)")
                                    
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
                        if not fallback_warned:
                            sys.stderr.write(
                                f"\n⚠️ [submit_job] Route canonique /job_logs/{job_id} introuvable (HTTP 404). "
                                f"Bascule de repli vers /api/jobs/{job_id}/logs.\n"
                            )
                            fallback_warned = True
                        h_resp2 = requests.get(f"{headnode_url}/api/jobs/{job_id}/logs?offset={log_offset}", timeout=5)
                        if h_resp2.status_code == 200:
                            logs_resp = h_resp2
                except requests.exceptions.RequestException as e:
                    is_exhausted, err_msg = log_retry_tracker.record_error(
                        error=e,
                        endpoint_desc="job logs from headnode",
                        target_url=headnode_url,
                        job_id=job_id,
                    )
                    sys.stderr.write(err_msg)
                    if is_exhausted:
                        raise requests.exceptions.ConnectionError(err_msg) from e
                except Exception as unexpected_err:
                    sys.stderr.write(f"\n⚠️ Erreur inattendue polling logs headnode: {unexpected_err}\n")

            if logs_resp is None and worker_url:
                try:
                    logs_resp = requests.get(f"{worker_url}/job_logs/{job_id}?offset={log_offset}", timeout=5)
                except requests.exceptions.RequestException as e:
                    is_exhausted, err_msg = log_retry_tracker.record_error(
                        error=e,
                        endpoint_desc="job logs from worker",
                        target_url=worker_url or headnode_url,
                        job_id=job_id,
                    )
                    sys.stderr.write(err_msg)
                    if is_exhausted:
                        raise requests.exceptions.ConnectionError(err_msg) from e
                except Exception as unexpected_err:
                    sys.stderr.write(f"\n⚠️ Unexpected error polling worker logs: {unexpected_err}\n")

            if logs_resp and logs_resp.status_code == 200:
                log_retry_tracker.record_success()
                try:
                    logs_data = logs_resp.json()
                    new_logs = logs_data.get('logs', '')
                    if new_logs:
                        import re
                        if re.search(r'tué par le système \(OOM Killer\)|arrêté préventivement par le GPU Watchdog|tu\xe9 par le syst\xe8me \(OOM Killer\)|arr\xeat\xe9 pr\xe9ventivement par le GPU Watchdog|killed by system \(OOM Killer\)|preemptively stopped by GPU Watchdog|Exit code 137|Out of Memory|exited with -9', new_logs, re.IGNORECASE):
                            oom_detected = True
                        if not status_printed:
                            print(f"\n\n[Streaming logs for job {job_id}]")
                            status_printed = True
                        formatted_chunks = []
                        for l in new_logs.splitlines(keepends=True):
                            has_nl = l.endswith("\n")
                            content_no_nl = l[:-1] if has_nl else l
                            formatted = format_multi_machine_log_line(content_no_nl, nodes_data)
                            formatted_chunks.append(formatted + ("\n" if has_nl else ""))
                        sys.stdout.write("".join(formatted_chunks))
                        sys.stdout.flush()
                        log_offset = logs_data.get('offset', log_offset)
                except (ValueError, KeyError) as json_err:
                    sys.stderr.write(f"\n⚠️ Invalid log format received from headnode/worker: {json_err}\n")
                except Exception as unexpected_err:
                    sys.stderr.write(f"\n⚠️ Unexpected error processing logs: {unexpected_err}\n")

            if status == 'completed':
                print_final_dag_summary(nodes_data, job_id)
                print(f"\n✅ Job {job_id} completed successfully!")
                return 0
            elif status == 'failed':
                print_final_dag_summary(nodes_data, job_id)
                exit_code = job.get('exit_code')
                if exit_code is None or exit_code == 0:
                    exit_code = 1  # Ensure non-zero exit on failure

                # A17 / v3 : Remonter fidèlement la cause réelle d'échec
                job_error = job.get('error_message') or job.get('error')
                has_node_failure = False

                if nodes_data and isinstance(nodes_data, list):
                    failed_nodes = [
                        nd for nd in nodes_data
                        if isinstance(nd, dict) and nd.get('status') == 'failed'
                    ]
                    done_nodes = [
                        nd for nd in nodes_data
                        if isinstance(nd, dict) and nd.get('status') in ('done', 'completed')
                    ]
                    running_nodes = [
                        nd for nd in nodes_data
                        if isinstance(nd, dict) and nd.get('status') == 'running'
                    ]
                    blocked_nodes = [
                        nd for nd in nodes_data
                        if isinstance(nd, dict) and nd.get('status') == 'blocked'
                    ]
                    pending_nodes = [
                        nd for nd in nodes_data
                        if isinstance(nd, dict) and nd.get('status') == 'pending'
                    ]

                    if failed_nodes:
                        has_node_failure = True
                        for nd in failed_nodes:
                            n_name = nd.get('name') or nd.get('node_name') or 'unknown'
                            n_err = nd.get('error_message') or f"non-zero exit code ({nd.get('exit_code', 'unknown')})"
                            print(f"❌ Failed node: '{n_name}' -> {n_err}")

                    # Détection spécifique : aucun nœud n'a démarré dans le DAG v3
                    if not done_nodes and not running_nodes and not failed_nodes and (blocked_nodes or pending_nodes):
                        has_node_failure = True
                        cause_desc = job_error or (
                            f"DAG plan interrupted before start ({len(blocked_nodes)} blocked, {len(pending_nodes)} pending out of {len(nodes_data)} node(s)); "
                            f"verify that submitted dvc.yaml corresponds to stages in target repo"
                        )
                        print(f"\n❌ v3 job failed: no node started: {cause_desc}")

                if job_error and not has_node_failure:
                    print(f"\n❌ Error message: {job_error}")

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
                    print(f"\n❌ Error: Job exceeded allocated REQUIRED_RAM limit ({ram_required} GB) and was killed by system (OOM Killer). Please increase this limit in .cluster-ci")
                elif exit_code == 255:
                    print(f"\n❌ Critical Failure: Job {job_id} execution process aborted unexpectedly (Exit code 255).")
                elif exit_code < 0:
                    sig = -exit_code
                    sig_desc = "SIGKILL (forcefully killed / external stop)" if sig == 9 else ("SIGTERM (interrupted / cancellation requested)" if sig == 15 else f"signal {sig}")
                    if not has_node_failure and not job_error:
                        print(f"\n❌ Job {job_id} interrupted: executing process stopped by {sig_desc} (exit code {exit_code}).")
                    else:
                        print(f"ℹ️  Executing process stopped by {sig_desc} (exit code {exit_code}).")
                else:
                    if not job_error and not has_node_failure:
                        print(f"\n❌ Job {job_id} failed with exit code {exit_code}")
                return exit_code

            if not status_printed and status != last_status:
                sys.stdout.write(f"Status: {status}...\n")
                sys.stdout.flush()
                last_status = status

        except requests.exceptions.RequestException as e:
            if log_retry_tracker.is_exhausted:
                raise
            is_exhausted, err_msg = status_retry_tracker.record_error(
                error=e,
                endpoint_desc="job status from headnode",
                target_url=headnode_url,
                job_id=job_id,
            )
            sys.stderr.write(err_msg)
            if is_exhausted:
                raise requests.exceptions.ConnectionError(err_msg) from e
        except Exception as e:
            sys.stderr.write(f"\n⚠️ Unexpected error checking status: {e}\n")
            raise

        sleep_int = max(log_retry_tracker.get_sleep_interval(), status_retry_tracker.get_sleep_interval())
        time.sleep(sleep_int)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Submit a job to Cluster-CI Scheduler")
    parser.add_argument("repo", nargs="?", default=None, help="Target repository (owner/repo)")
    parser.add_argument("branch", nargs="?", default=None, help="Target branch")
    parser.add_argument("--attach", default=None, help="Re-attach to an existing job ID and stream its logs")
    parser.add_argument("--headnode", default=os.environ.get("HEADNODE_URL"), help="Headnode URL")
    parser.add_argument("--gh-token", default=None, help="GitHub token for cloning private repos")
    parser.add_argument("--local", action="store_true", help="Submit local directory without git clone")
    parser.add_argument("--local-repo-path", default=None, help="Path to local repository")
    parser.add_argument("--repo-dir", default=None, help="Path to target repository containing dvc.yaml")
    parser.add_argument("-e", "--env", action="append", default=[], help="Environment variables (KEY=VAL)")

    args = parser.parse_args()

    if not args.headnode:
        print("Error: HEADNODE_URL environment variable is missing and no --headnode argument was provided.")
        print("   Please set the HEADNODE_URL environment variable or provide the --headnode parameter.")
        sys.exit(1)

    if args.attach:
        exit_code = wait_for_job(args.headnode, args.attach, branch=args.branch)
        sys.exit(exit_code)

    if not args.repo or not args.branch:
        parser.error("the following arguments are required: repo, branch (or provide --attach <job_id>)")

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
        is_local=args.local, local_repo_path=local_repo_path, repo_dir=args.repo_dir
    )
    exit_code = wait_for_job(args.headnode, job_id, branch=args.branch)
    sys.exit(exit_code)
