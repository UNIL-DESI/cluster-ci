#!/usr/bin/env python3
"""Cluster-CI Run CLI

Helps researchers submit jobs via "Shadow Push" to a draft branch.
Compatible with Windows, macOS, and Linux.
"""

import sys
import os
import re
import time
import json
import atexit
import signal
import tempfile
import argparse
import subprocess
import threading
import queue
import urllib.request
import urllib.error

# Global variables for cleanup
RUN_ID = None
BRANCH = None
COMMIT_SHA = None
USER_INTERRUPTED = False
REPO_FULL_NAME = "UNIL-DESI/cluster-ci"
STATE_FILE = ".cluster-ci-run.json"
_CLEANUP_DONE = False

def discover_headnode_url():
    """Discover the headnode URL from environment or local .env file."""
    # 1. Direct env var
    url = os.environ.get("HEADNODE_URL")
    if url:
        return url
    # 2. Parse .env file in repo root for HEADNODE_IP
    env_file = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("HEADNODE_IP="):
                        ip = line.split("=", 1)[1].strip().strip('"').strip("'")
                        return f"http://{ip}:5000"
        except Exception:
            pass
    return None

def find_job_id_from_headnode(headnode_url, repo, branch):
    """Query the headnode to find the active job_id for a given repo+branch."""
    if not headnode_url:
        return None
    try:
        url = f"{headnode_url}/api/projects/{repo}/runs"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            runs = json.loads(resp.read().decode())
            for run in runs:
                if run.get("status") in ("running", "assigned", "pending") and run.get("branch") == branch:
                    return run.get("job_id")
    except Exception:
        pass
    return None

def print_line(line, force=False):
    if not line:
        return
    line = line.strip()
    if not line:
        return

    # Skip tmux status bar lines (e.g. '0:bash*   ...')
    if re.match(r"^\d+:.*\*\s", line) or "bash*" in line:
        return
    # Skip script header/footer and SSH connection status messages
    if line.startswith("Script ") and ("started" in line or "done" in line):
        return
    if "Connection to" in line and "closed" in line:
        return
    if "[server exited]" in line or "[lost server]" in line:
        return
    if "size 80x23 from a smaller client" in line:
        return
    
    # Skip DVC progress bar fragments and artifacts
    if line == "!" or line.startswith("! ") or line.startswith("Checking out"):
        return
    if "file/s]" in line or "files/s]" in line or "B/s]" in line:
        return
    if re.match(r"^Checking out .+:\s+\d+%", line):
        return

    print(line, flush=True)

def check_dependencies():
    """Verify that gh and git are installed and accessible."""
    # Robust PATH check for Windows: add standard GitHub CLI path if not present but exists
    if sys.platform == "win32":
        standard_path = r"C:\Program Files\GitHub CLI"
        if os.path.exists(os.path.join(standard_path, "gh.exe")):
            paths = os.environ.get("PATH", "").split(os.pathsep)
            if standard_path not in paths:
                os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + standard_path

    try:
        subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ Error: git is not installed or not in PATH.", file=sys.stderr)
        sys.exit(1)

    try:
        subprocess.run(["gh", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ Error: github-cli (gh) is not installed.", file=sys.stderr)
        print("Please install it: https://cli.github.com/", file=sys.stderr)
        sys.exit(1)

    # Check if in a git repository
    res = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0 or res.stdout.strip() != "true":
        print("❌ Error: Not in a git repository.", file=sys.stderr)
        sys.exit(1)

def check_gh_auth():
    """Ensure user is logged in to GitHub CLI."""
    res = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        print("🔐 GitHub CLI not authenticated. Starting login...")
        subprocess.run(["gh", "auth", "login"], check=True)

def get_current_user():
    """Retrieve GitHub username."""
    res = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    return res.stdout.strip()

def get_repo_full_name():
    """Find the GitHub repository name from remote origin URL."""
    global REPO_FULL_NAME
    try:
        res = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        url = res.stdout.strip()
        # Extract owner/repo from URL (HTTPS or SSH)
        match = re.search(r"github\.com[:/]([^/]+/[^/.]+)(?:\.git)?", url)
        if match:
            REPO_FULL_NAME = match.group(1)
    except Exception:
        # Fallback to default
        pass
    return REPO_FULL_NAME

def save_run_state(run_id, branch, commit_sha, job_id=None, headnode_url=None):
    """Persist active run info to a state file for orphan detection."""
    try:
        state = {
            "run_id": run_id, "branch": branch, "commit_sha": commit_sha,
            "pid": os.getpid(), "job_id": job_id, "headnode_url": headnode_url,
            "cluster_token": os.environ.get("CLUSTER_TOKEN"),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

def clear_run_state():
    """Remove the state file after successful cleanup."""
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass

def _headnode_stop_job(job_id, headnode_url, cluster_token):
    """Contact the headnode to stop a job (kills Docker containers, releases RAM)."""
    if not job_id or not headnode_url:
        return
    try:
        url = f"{headnode_url}/api/jobs/{job_id}/stop"
        data = json.dumps({}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if cluster_token:
            req.add_header("Authorization", f"Bearer {cluster_token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            print(f"   Headnode: {result.get('message', 'Job stopped')}")
    except urllib.error.HTTPError as e:
        # 404 means job already cleaned up — that's fine
        if e.code != 404:
            print(f"   Headnode stop failed (HTTP {e.code}): {e.reason}")
    except Exception as e:
        print(f"   Could not reach headnode: {e}")

def cancel_and_cleanup_run(run_id, branch, commit_sha=None, job_id=None, headnode_url=None, cluster_token=None):
    """Cancel a GHA run, sync partial results, and delete the draft branch.
    
    This is the single source of truth for run teardown, used by:
    - Normal cleanup after shadow_run()
    - Orphan recovery at startup
    - Signal handlers (Ctrl+C, SIGTERM)
    """
    # 0. Stop job on headnode FIRST (kills Docker containers, releases worker RAM)
    if job_id and headnode_url:
        print("🔌 Stopping job on cluster headnode...")
        _headnode_stop_job(job_id, headnode_url, cluster_token)

    # 1. Sync partial results before destroying anything
    try:
        print("📥 Syncing any partial results before cleanup...")
        fetch_cluster_results(branch, commit_sha)
    except Exception:
        pass

    # 2. Cancel the GHA run if still active
    if run_id:
        try:
            res = subprocess.run(
                ["gh", "run", "view", str(run_id), "--json", "status"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if res.returncode == 0:
                status = json.loads(res.stdout).get("status")
                if status not in ("completed", "success", "failure", "cancelled"):
                    print(f"🛑 Cancelling GitHub Actions run {run_id}...")
                    subprocess.run(
                        ["gh", "run", "cancel", str(run_id)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
        except Exception:
            pass

    # 3. Delete the draft branch
    if branch:
        print(f"🧹 Deleting remote branch origin/{branch}...")
        res = subprocess.run(
            ["git", "push", "origin", "--delete", branch, "--quiet"],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if res.returncode == 0:
            print("✅ Remote branch deleted successfully.")
        else:
            if "not found" in res.stderr.lower() or "does not exist" in res.stderr.lower() or "failed to push some refs" in res.stderr.lower():
                print("ℹ️  Remote branch was already deleted or did not exist.")
            else:
                print(f"⚠️  Could not delete remote branch: {res.stderr.strip()}")

    # 4. Remove state file
    clear_run_state()

def cleanup():
    """Cleanup handler: sync results, cancel run, delete branch."""
    global _CLEANUP_DONE
    if _CLEANUP_DONE:
        return
    _CLEANUP_DONE = True
    if BRANCH:
        # Load job_id and headnode_url from state file for headnode contact
        job_id = None
        headnode_url = None
        cluster_token = None
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                    job_id = state.get("job_id")
                    headnode_url = state.get("headnode_url")
                    cluster_token = state.get("cluster_token")
        except Exception:
            pass
        cancel_and_cleanup_run(
            RUN_ID if USER_INTERRUPTED else None, BRANCH, COMMIT_SHA,
            job_id=job_id if USER_INTERRUPTED else None,
            headnode_url=headnode_url if USER_INTERRUPTED else None,
            cluster_token=cluster_token if USER_INTERRUPTED else None,
        )

def recover_orphaned_run():
    """Detect and clean up a run left behind by a force-killed process."""
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        orphan_run_id = state.get("run_id")
        orphan_branch = state.get("branch")
        orphan_sha = state.get("commit_sha")
        orphan_pid = state.get("pid")

        # Check if the process that wrote this state is still alive
        if orphan_pid:
            try:
                os.kill(orphan_pid, 0)  # signal 0 = check existence
                # Process is still alive — this is not an orphan
                return
            except (OSError, ProcessLookupError):
                pass  # Process is dead — this IS an orphan

        print(f"\n⚠️  Detected orphaned run from a previous force-killed session.")
        print(f"   Run ID: {orphan_run_id} | Branch: {orphan_branch}")
        print(f"   Cleaning up automatically...")
        orphan_job_id = state.get("job_id")
        orphan_headnode_url = state.get("headnode_url")
        orphan_cluster_token = state.get("cluster_token")
        cancel_and_cleanup_run(
            orphan_run_id, orphan_branch, orphan_sha,
            job_id=orphan_job_id, headnode_url=orphan_headnode_url,
            cluster_token=orphan_cluster_token,
        )
        print("✅ Orphaned run cleaned up.\n")
    except Exception as e:
        print(f"⚠️  Could not recover orphaned run state: {e}", file=sys.stderr)
        clear_run_state()

def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT to ensure cleanup runs on termination."""
    global USER_INTERRUPTED
    USER_INTERRUPTED = True
    print(f"\n🛑 Received signal {signum}, cleaning up...")
    cleanup()
    sys.exit(128 + signum)

def check_curl():
    """Verify if curl is installed."""
    try:
        subprocess.run(["curl", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, Exception):
        return False

def display_clean_queue_status(run_id):
    """Fetches GHA logs, parses them to find the latest queue/scheduler status block,
    clears the terminal screen, and prints a clean, non-duplicated view.
    """
    try:
        # Run gh run view --log and capture the output
        res = subprocess.run(["gh", "run", "view", str(run_id), "--log"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            return False

        lines = res.stdout.splitlines()
        log_lines = []
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 3:
                content = parts[2]
                # Clean GHA noise
                content = content.replace("\ufeff", "")
                content = re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ", "", content)
                content = content.replace("##[group]", "▶️  ").replace("##[endgroup]", "")
                log_lines.append(content.rstrip())
            elif len(parts) == 1:
                # Sometimes logs don't have tabs if they are already formatted or from fallback
                content = parts[0]
                content = content.replace("\ufeff", "")
                content = re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ", "", content)
                log_lines.append(content.rstrip())

        # Find the latest queue block
        # A queue block is delimited by lines with '════' or '═══' (at least 20 '=' or '═')
        # Let's extract the last occurrence of such a block
        queue_start_idx = -1
        queue_end_idx = -1
        
        # Let's scan from the end to find the last queue block
        for i in range(len(log_lines) - 1, -1, -1):
            line = log_lines[i]
            if "⏳ FILE D'ATTENTE CLUSTER-CI" in line:
                queue_start_idx = i
                # Look for the preceding boundary or just use i-1
                if i > 0 and ("═" in log_lines[i-1] or "===" in log_lines[i-1]):
                    queue_start_idx = i - 1
                break

        if queue_start_idx != -1:
            # Look for the end of this block
            for j in range(queue_start_idx + 1, len(log_lines)):
                if "═" in log_lines[j] and len(log_lines[j].strip()) >= 20:
                    queue_end_idx = j
                    break
            if queue_end_idx == -1:
                queue_end_idx = len(log_lines) - 1

        # Clear screen properly on Windows and Linux/macOS
        os.system('cls' if os.name == 'nt' else 'clear')

        print("═"*80)
        print(f"  🖥️  CLUSTER-CI DYNAMIC QUEUE MONITOR (Run ID: {run_id})")
        print(f"  Last updated: {time.strftime('%H:%M:%S')} (Auto-refreshes every 15s)")
        print("═"*80)

        if queue_start_idx != -1:
            # Print the extracted block
            for idx in range(queue_start_idx, queue_end_idx + 1):
                print(log_lines[idx])
        else:
            # If no queue block found, print the last 20 lines of log to show startup
            print("⏳ Initializing environment on the cluster... (no queue data yet)")
            print("\n--- Recent Logs ---")
            start_idx = max(0, len(log_lines) - 20)
            for idx in range(start_idx, len(log_lines)):
                print(log_lines[idx])
            print("-------------------")
        
        print("═"*80)
        print("💡 TIP: Press Ctrl+C to cancel the job and clean up the remote branch.")
        return True
    except Exception:
        # Fallback to simple print if something goes wrong
        return False

def stream_logs(run_id, commit_sha):
    """Monitor GHA run and capture live log stream via piping or fallback API."""
    has_curl = check_curl()
    last_gha_poll_time = 0
    
    # For 'view' mode or resuming, it's good to show what happened before
    print(f"📦 Fetching latest logs from GitHub Actions (Run ID: {run_id})...")
    try:
        log_res = subprocess.run(["gh", "run", "view", str(run_id), "--log"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if log_res.returncode == 0:
            lines = log_res.stdout.splitlines()
            # Show last 20 lines of previous logs to give context
            for line in lines[-20:]:
                parts = line.split("\t")
                content = parts[2] if len(parts) >= 3 else line
                content = content.replace("\ufeff", "")
                content = re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ", "", content)
                content = content.replace("##[group]", "▶️  ").replace("##[endgroup]", "")
                if content.strip():
                    print(f"\033[90m[context]\033[0m {content}")
    except Exception:
        pass

    try:
        q = queue.Queue()
        proc = None
        received_data = False

        if has_curl and commit_sha:
            proc = subprocess.Popen(
                ["curl", "-s", "-N", f"https://ppng.io/cluster-ci-log-{commit_sha}"],
                stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1
            )
            
            def log_reader_thread():
                for line in proc.stdout:
                    q.put(line)
                    
            reader_thread = threading.Thread(target=log_reader_thread, daemon=True)
            reader_thread.start()

        while True:
            try:
                line = q.get(timeout=1)
                line_stripped = line.rstrip('\r\n')
                if "has been established already" in line_stripped:
                    continue
                if not received_data:
                    print("\n🟢 Live stream connected.")
                    received_data = True
                print_line(line_stripped, force=True)
                last_gha_poll_time = time.time()
            except queue.Empty:
                if time.time() - last_gha_poll_time > 5:
                    status = "queued"
                    conclusion = None
                    try:
                        res = subprocess.run(["gh", "run", "view", str(run_id), "--json", "status,conclusion,url"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                        if res.returncode == 0:
                            info = json.loads(res.stdout)
                            status = info.get("status")
                            conclusion = info.get("conclusion")
                            url = info.get("url", "URL non disponible")
                            if status == "completed" or conclusion:
                                if proc:
                                    try: proc.terminate()
                                    except: pass
                                if conclusion == "success":
                                    print("\n✅ Cluster-CI run completed successfully!")
                                    return 0
                                elif conclusion == "cancelled":
                                    print("\n⚠️ [ERREUR] L'exécution a été annulée.")
                                    print("❓ POURQUOI : Raisons fréquentes (nouvelle commande lancée annulant l'ancienne, timeout, ou annulation manuelle).")
                                    print(f"🔧 COMMENT RÉSOUDRE : Consultez les logs distants : {url}")
                                    return 1
                                else:
                                    print(f"\n❌ [ERREUR] L'exécution s'est terminée avec le statut : {conclusion or 'failed'}")
                                    print("❓ POURQUOI : Une erreur est survenue pendant l'exécution (problème de dépendance, erreur dans le code, ou défaillance de l'infrastructure).")
                                    print(f"🔧 COMMENT RÉSOUDRE : Consultez les logs distants pour voir la trace d'erreur complète : {url}")
                                    return 1
                    except Exception:
                        pass

                    if not received_data:
                        if not display_clean_queue_status(run_id):
                            print("\r⏳ En attente de l'allocation d'un runner GitHub Actions...", end="", flush=True)
                    
                    last_gha_poll_time = time.time()

    except KeyboardInterrupt:
        if 'proc' in locals() and proc:
            try: proc.terminate()
            except: pass
        raise

def check_gitattributes_safety():
    """Verify that .gitattributes is safe and doesn't corrupt binary files."""
    if not os.path.exists(".gitattributes"):
        return

    try:
        with open(".gitattributes", "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        has_global_text = False
        declared_binaries = set()

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Check if global text normalization is active
            if re.match(r"^\*\s+text(\s+|$|=)", line):
                has_global_text = True
            
            # Check for binary file extensions declared
            match = re.match(r"^([\w\.\*\-\?]+)\s+(binary|-text)", line)
            if match:
                pattern = match.group(1)
                # Normalize extension (e.g. *.png -> .png)
                ext = pattern.replace("*", "")
                declared_binaries.add(ext)

        if has_global_text:
            critical_extensions = {".png", ".jpg", ".jpeg", ".gif", ".npy", ".pkl", ".cluster", ".pt"}
            missing_extensions = critical_extensions - declared_binaries
            
            if missing_extensions:
                print("⚠️  [SAFETY WARNING] Global text normalization is enabled in .gitattributes (* text),")
                print("   but the following critical binary file extensions are NOT protected:")
                print(f"   {', '.join(sorted(missing_extensions))}")
                print("   This will corrupt these files during automatic Git synchronization from the cluster!")
                print("   Fixing .gitattributes automatically...")
                
                # Append missing protections to .gitattributes
                with open(".gitattributes", "a", encoding="utf-8") as f:
                    f.write("\n# Added automatically by cluster-run to prevent binary file corruption under global text normalization\n")
                    for ext in sorted(missing_extensions):
                        f.write(f"*{ext} binary\n")
                
                print("✅ .gitattributes updated with safety locks for binary files.")
    except Exception as e:
        print(f"⚠️  Could not verify .gitattributes safety: {e}")

def clean_old_results():
    """Purge old results and logs before execution to prevent stale data."""
    import glob
    patterns = [
        "results/**/*.log",
        "results/**/*.json",
        "results/**/*.csv",
        "results/**/*.txt",
        "results/**/*.pt",
        "artifacts/**/*.log",
        "artifacts/**/*.json",
        "artifacts/**/*.csv",
        "artifacts/**/*.txt",
        "artifacts/**/*.png",
        "artifacts/**/*.pt",
        "metrics/**/*.log",
        "metrics/**/*.json",
        "logs/**/*.log"
    ]
    
    removed = 0
    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed += 1
                except Exception:
                    pass
                    
    if removed > 0:
        print(f"🧹 Purged {removed} old result file(s) to ensure a fresh start.")

def fetch_cluster_results(branch, commit_sha=None):
    """Fetch and checkout files modified by the cluster from the draft branch.

    Returns True if sync succeeded, False otherwise.
    """
    try:
        # 1. Fetch the latest commits on the draft branch
        subprocess.run(
            ["git", "fetch", "origin", branch],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        # 2. Determine base ref for diff
        base_ref = commit_sha if commit_sha else "HEAD"

        # 3. Detect files modified by the execution on the cluster
        res_diff = subprocess.run(
            ["git", "diff", base_ref, f"origin/{branch}", "--name-only", "--diff-filter=AM"],
            capture_output=True, text=True,
        )
        if res_diff.returncode != 0:
            print(
                f"⚠️  Could not diff against origin/{branch} (exit code {res_diff.returncode}).",
                file=sys.stderr,
            )
            return False

        cluster_files = []
        for line in res_diff.stdout.splitlines():
            name = line.strip()
            if name and not name.startswith(".cluster-ci"):
                cluster_files.append(name)

        if cluster_files:
            print("📂 Auto-syncing updated DVC results and metrics from cluster:")
            for f in cluster_files:
                print(f"   - {f}")
            # Force checkout of these files, overwriting any local copy
            subprocess.run(
                ["git", "checkout", f"origin/{branch}", "--"] + cluster_files,
                check=True,
            )
            print("✅ Local workspace synchronized with cluster results successfully!")
        else:
            print("ℹ️ No changes in metrics, plots or dvc.lock detected on the cluster.")
        return True
    except Exception as e:
        print(f"⚠️  Failed to auto-sync results from cluster: {e}", file=sys.stderr)
        return False


def shadow_run():
    """Package current workspace changes, shadow commit, shadow push, and stream logs."""
    global RUN_ID, BRANCH, COMMIT_SHA, USER_INTERRUPTED
    
    clean_old_results()
    
    check_gitattributes_safety()
    check_gh_auth()
    user = get_current_user()
    BRANCH = f"cluster-draft/{user}"

    print(f"🏗️  Preparing shadow push for user: {user} (including untracked files)")

    # Create temporary file for Git index to prevent polluting the user's workspace index
    fd, temp_index_path = tempfile.mkstemp()
    os.close(fd)
    
    commit_sha = None
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = temp_index_path

    try:
        # 1. git read-tree HEAD
        subprocess.run(["git", "read-tree", "HEAD"], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 2. git add --all (adds tracked, modified, and untracked files)
        subprocess.run(["git", "add", "--all"], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 3. git write-tree
        res_tree = subprocess.run(["git", "write-tree"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        tree = res_tree.stdout.strip()
        # 4. git commit-tree tree -p HEAD -m "Shadow commit..."
        res_commit = subprocess.run(
            ["git", "commit-tree", tree, "-p", "HEAD", "-m", f"Shadow commit for {user}"],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
        )
        commit_sha = res_commit.stdout.strip()
        COMMIT_SHA = commit_sha
    finally:
        try:
            os.remove(temp_index_path)
        except Exception:
            pass

    if not commit_sha:
        print("❌ Error: Failed to create shadow commit.", file=sys.stderr)
        sys.exit(1)

    # Detect the last active GHA run ID before pushing to avoid checking a stale run
    last_known_run_id = None
    try:
        res = subprocess.run(["gh", "run", "list", "--branch", BRANCH, "--limit", "1", "--json", "databaseId"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode == 0:
            runs = json.loads(res.stdout)
            if runs:
                last_known_run_id = runs[0].get("databaseId")
    except Exception:
        pass

    print(f"🚀 Shadow pushing to origin/{BRANCH}...")
    subprocess.run(["git", "push", "origin", f"{commit_sha}:refs/heads/{BRANCH}", "--force", "--quiet"], check=True)

    # Find the triggered GHA run
    print("⏳ Waiting for GitHub Actions to trigger...")
    time.sleep(4)
    run_id = None
    
    for attempt in range(15):
        try:
            res = subprocess.run(["gh", "run", "list", "--branch", BRANCH, "--limit", "1", "--json", "databaseId,status"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode == 0:
                runs = json.loads(res.stdout)
                if runs:
                    curr_id = runs[0].get("databaseId")
                    curr_status = runs[0].get("status")
                    if curr_id != last_known_run_id and curr_status != "completed":
                        run_id = curr_id
                        break
        except Exception:
            pass
        time.sleep(2)

    # Fallback to the latest run on the branch if we couldn't find a freshly triggered one
    if not run_id:
        try:
            res = subprocess.run(["gh", "run", "list", "--branch", BRANCH, "--limit", "1", "--json", "databaseId"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode == 0:
                runs = json.loads(res.stdout)
                if runs and runs[0].get("databaseId") != last_known_run_id:
                    run_id = runs[0].get("databaseId")
        except Exception:
            pass

    if not run_id:
        print("❌ Error: Could not find the triggered workflow run.", file=sys.stderr)
        sys.exit(1)

    RUN_ID = run_id

    # Persist run state so orphan recovery works if we are force-killed
    headnode_url = discover_headnode_url()
    job_id = find_job_id_from_headnode(headnode_url, REPO_FULL_NAME, BRANCH) if headnode_url else None
    cluster_token = os.environ.get("CLUSTER_TOKEN")
    # Read CLUSTER_TOKEN from .env if not in environment
    if not cluster_token:
        env_file = os.path.join(os.getcwd(), ".env")
        if os.path.exists(env_file):
            try:
                with open(env_file, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("CLUSTER_TOKEN="):
                            cluster_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
            except Exception:
                pass
    if cluster_token:
        os.environ["CLUSTER_TOKEN"] = cluster_token
    save_run_state(run_id, BRANCH, commit_sha, job_id=job_id, headnode_url=headnode_url)

    print(f"📺 Streaming logs for run {run_id} (Ctrl+C to cancel)...")
    
    try:
        stream_logs(run_id, commit_sha)
    except KeyboardInterrupt:
        USER_INTERRUPTED = True
        print("\n🛑 Execution interrupted by user.")
        # cleanup() (called via finally in main) will sync results,
        # cancel the GHA run, and delete the branch.
        sys.exit(130)

    # Check final status
    conclusion = "unknown"
    url = "URL non disponible"
    for _ in range(5):
        try:
            res = subprocess.run(["gh", "run", "view", str(run_id), "--json", "conclusion,url"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode == 0:
                info = json.loads(res.stdout)
                conclusion = info.get("conclusion", "unknown")
                url = info.get("url", "URL non disponible")
                if conclusion and conclusion != "null":
                    break
        except Exception:
            pass
        time.sleep(1)

    # Always sync results back, regardless of success or failure.
    # Even on failure, earlier stages may have produced valuable metrics,
    # plots, and dvc.lock updates that must be preserved locally.
    print("📥 Fetching updated results (metrics, plots, dvc.lock) from cluster...")
    sync_success = fetch_cluster_results(BRANCH, COMMIT_SHA)
    clear_run_state()

    if conclusion != "success":
        if sync_success:
            print("ℹ️  Results from completed stages have been synced despite the failure.")
        print(f"❌ Run finished with conclusion: {conclusion}")
        sys.exit(1)

def main():
    # Force standard output streams to use UTF-8 to prevent UnicodeEncodeError under Windows CMD/PowerShell
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):  # Windows Ctrl+Break
        signal.signal(signal.SIGBREAK, _signal_handler)
    atexit.register(cleanup)

    parser = argparse.ArgumentParser(description="Cluster-CI Command Line Interface")
    parser.add_argument("command", nargs="?", default=None, choices=["list", "view", "cancel", "sync"],
                        help="Action to perform (default: submit a new shadow run)")
    parser.add_argument("run_id", nargs="?", default=None,
                        help="Target GHA run ID for 'view' or 'cancel'")

    args = parser.parse_args()

    check_dependencies()

    # Recover any orphaned run from a previously force-killed session
    recover_orphaned_run()

    if args.command == "list":
        subprocess.run(["gh", "run", "list", "--workflow", "Cluster-CI Execution"])
    
    elif args.command == "view":
        run_id = args.run_id
        if not run_id:
            check_gh_auth()
            user = get_current_user()
            branch = f"cluster-draft/{user}"
            try:
                res = subprocess.run(["gh", "run", "list", "--branch", branch, "--limit", "1", "--json", "databaseId"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                runs = json.loads(res.stdout) if res.returncode == 0 else []
                if runs:
                    run_id = str(runs[0].get("databaseId"))
            except Exception:
                pass
            
            if not run_id:
                print("Usage: cluster-run view <run_id>", file=sys.stderr)
                sys.exit(1)
        
        # Check run status and headSha to determine if we can stream live logs
        try:
            res = subprocess.run(["gh", "run", "view", str(run_id), "--json", "status,headSha"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode == 0:
                info = json.loads(res.stdout)
                status = info.get("status")
                head_sha = info.get("headSha")
                if status in ("in_progress", "queued"):
                    # We can always call stream_logs, it will handle fallback if head_sha is missing
                    try:
                        stream_logs(run_id, head_sha)
                    except KeyboardInterrupt:
                        print("\n🛑 Stream interrupted by user.")
                    return
        except Exception:
            pass

        # Fallback to historical logs if completed
        subprocess.run(["gh", "run", "view", str(run_id), "--log"])

    elif args.command == "sync":
        check_gh_auth()
        user = get_current_user()
        branch = f"cluster-draft/{user}"
        print(f"📥 Manual sync from origin/{branch}...")
        success = fetch_cluster_results(branch)
        if not success:
            sys.exit(1)

    elif args.command == "cancel":
        run_id = args.run_id
        check_gh_auth()
        user = get_current_user()
        branch = f"cluster-draft/{user}"
        
        if not run_id:
            try:
                res = subprocess.run(["gh", "run", "list", "--branch", branch, "--limit", "1", "--json", "databaseId"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                runs = json.loads(res.stdout) if res.returncode == 0 else []
                if runs:
                    run_id = str(runs[0].get("databaseId"))
            except Exception:
                pass
                
            if not run_id:
                print("Usage: cluster-run cancel <run_id>", file=sys.stderr)
                sys.exit(1)

        # 0. Stop job on headnode FIRST (kills Docker, releases resources)
        headnode_url = discover_headnode_url()
        repo = get_repo_full_name()
        if headnode_url:
            job_id = None
            # Try to read job_id from state file
            try:
                if os.path.exists(STATE_FILE):
                    with open(STATE_FILE, "r", encoding="utf-8") as f:
                        state = json.load(f)
                        job_id = state.get("job_id")
            except Exception:
                pass
            # Fallback: query headnode by branch
            if not job_id:
                job_id = find_job_id_from_headnode(headnode_url, repo, branch)
            if job_id:
                print(f"🔌 Stopping job {job_id[:12]}... on cluster headnode...")
                cluster_token = os.environ.get("CLUSTER_TOKEN")
                if not cluster_token:
                    try:
                        if os.path.exists(".env"):
                            with open(".env", "r", encoding="utf-8", errors="replace") as f:
                                for line in f:
                                    if line.strip().startswith("CLUSTER_TOKEN="):
                                        cluster_token = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                    except Exception:
                        pass
                _headnode_stop_job(job_id, headnode_url, cluster_token)
            else:
                print("⚠️  Could not find job_id to stop on headnode. Proceeding with GHA cancel only.")
        else:
            print("⚠️  Headnode URL not found. Proceeding with GHA cancel only.")

        # 1. Cancel the GHA run
        print(f"🛑 Cancelling run {run_id}...")
        subprocess.run(["gh", "run", "cancel", run_id])

        # 2. Delete the draft branch
        print(f"🧹 Deleting remote branch origin/{branch}...")
        res = subprocess.run(["git", "push", "origin", "--delete", branch, "--quiet"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode == 0:
            print("✅ Remote branch deleted successfully.")
        else:
            if "not found" in res.stderr.lower() or "does not exist" in res.stderr.lower() or "failed to push some refs" in res.stderr.lower():
                print("ℹ️  Remote branch was already deleted or did not exist.")
            else:
                print(f"⚠️  Could not delete remote branch: {res.stderr.strip()}")

        # 3. Clear state file
        clear_run_state()

    else:
        # Submit shadow run
        try:
            shadow_run()
        finally:
            cleanup()

if __name__ == "__main__":
    main()
