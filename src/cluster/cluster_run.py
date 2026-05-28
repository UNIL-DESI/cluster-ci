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
import tempfile
import argparse
import subprocess
import threading

# Global variables for cleanup
RUN_ID = None
BRANCH = None
COMMIT_SHA = None
USER_INTERRUPTED = False
REPO_FULL_NAME = "UNIL-DESI/cluster-ci"

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

def cleanup():
    """Remove draft branch and cancel active workflow run if user interrupted."""
    global RUN_ID, BRANCH, USER_INTERRUPTED
    if BRANCH:
        if RUN_ID and USER_INTERRUPTED:
            # Check status of the GHA run
            try:
                res = subprocess.run(["gh", "run", "view", str(RUN_ID), "--json", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                if res.returncode == 0:
                    status_info = json.loads(res.stdout)
                    status = status_info.get("status")
                    if status not in ("completed", "success", "failure", "cancelled"):
                        print(f"\n🛑 Cancelling GitHub Actions run {RUN_ID}...")
                        subprocess.run(["gh", "run", "cancel", str(RUN_ID)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        
        print(f"🧹 Deleting remote branch origin/{BRANCH}...")
        subprocess.run(["git", "push", "origin", "--delete", BRANCH, "--quiet"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        consecutive_failures = 0
        MAX_RECONNECTS = 5

        while True:
            # 1. Check GHA status
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

            # 2. Try live streaming if possible
            if has_curl and commit_sha and consecutive_failures < MAX_RECONNECTS:
                proc = subprocess.Popen(
                    ["curl", "-s", "-N", f"https://ppng.io/cluster-ci-log-{commit_sha}"],
                    stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1
                )
                
                # Flag to check if we received any data
                received_data = False
                
                # Thread to monitor GHA status and kill curl if job finishes
                stop_curl_event = threading.Event()
                def kill_curl_if_done():
                    while not stop_curl_event.is_set() and proc.poll() is None:
                        time.sleep(5)
                        try:
                            if not received_data:
                                display_clean_queue_status(run_id)
                            r = subprocess.run(["gh", "run", "view", str(run_id), "--json", "status"], capture_output=True, text=True)
                            if r.returncode == 0 and json.loads(r.stdout).get("status") == "completed":
                                proc.terminate()
                                break
                        except: pass

                mon_thread = threading.Thread(target=kill_curl_if_done, daemon=True)
                mon_thread.start()

                try:
                    for line in proc.stdout:
                        line_stripped = line.rstrip('\r\n')
                        # Detect ppng.io connection error (stream is dead)
                        if "has been established already" in line_stripped:
                            consecutive_failures += 1
                            break
                        if not received_data:
                            print("🟢 Live stream connected.")
                            received_data = True
                            consecutive_failures = 0  # Reset on successful data
                        print_line(line_stripped, force=True)
                        last_gha_poll_time = time.time()
                except Exception:
                    pass
                finally:
                    stop_curl_event.set()
                    try:
                        proc.terminate()
                        proc.wait(timeout=1)
                    except: pass

                if not received_data:
                    consecutive_failures += 1

                if consecutive_failures >= MAX_RECONNECTS:
                    print(f"⚠️  Live stream unavailable after {MAX_RECONNECTS} attempts. Falling back to GHA polling.")
            
            # 3. Fallback / Idle Poll
            if time.time() - last_gha_poll_time > 10:
                display_clean_queue_status(run_id)
                last_gha_poll_time = time.time()
            
            # Exponential backoff on reconnection failures
            backoff = min(2 ** consecutive_failures, 10) if consecutive_failures > 0 else 2
            time.sleep(backoff)

    except KeyboardInterrupt:
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
    print(f"📺 Streaming logs for run {run_id} (Ctrl+C to cancel)...")
    
    try:
        stream_logs(run_id, commit_sha)
    except KeyboardInterrupt:
        USER_INTERRUPTED = True
        print("\n🛑 Execution interrupted by user.")
        # cleanup is called via sys.exit trigger
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

    if conclusion == "success":
        print("✅ Cluster-CI run completed successfully.")
        print("📥 Fetching updated results (metrics, plots, dvc.lock) from cluster...")
        try:
            # 1. Fetch the latest commits on the draft branch
            subprocess.run(["git", "fetch", "origin", BRANCH], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            
            # 2. Determine base ref for diff
            base_ref = COMMIT_SHA if COMMIT_SHA else "HEAD"
            
            # 3. Detect files modified by the execution on the cluster
            res_diff = subprocess.run(["git", "diff", base_ref, f"origin/{BRANCH}", "--name-only"], capture_output=True, text=True)
            if res_diff.returncode == 0:
                cluster_files = []
                for line in res_diff.stdout.splitlines():
                    name = line.strip()
                    if name and not name.startswith(".cluster-ci"):
                        cluster_files.append(name)
                
                if cluster_files:
                    print(f"📂 Auto-syncing updated DVC results and metrics from cluster:")
                    for f in cluster_files:
                        print(f"   - {f}")
                    # Force checkout of these files, overwriting any local copy
                    subprocess.run(["git", "checkout", f"origin/{BRANCH}", "--"] + cluster_files, check=True)
                    print("✅ Local workspace synchronized with cluster results successfully!")
                else:
                    print("ℹ️ No changes in metrics, plots or dvc.lock detected on the cluster.")
            else:
                raise RuntimeError(
                    f"FATAL: 'git diff {base_ref} origin/{BRANCH} --name-only' failed "
                    f"(exit code {res_diff.returncode}). Cannot determine which files "
                    f"were modified on the cluster. Stderr: {res_diff.stderr}"
                )
        except Exception as e:
            print(f"❌ Failed to auto-sync results from cluster: {e}", file=sys.stderr)
            raise
    elif conclusion == "cancelled":
        print("\n⚠️ [ERREUR] L'exécution a été annulée.")
        print("❓ POURQUOI : Raisons fréquentes (nouvelle commande lancée annulant l'ancienne, timeout, ou annulation manuelle).")
        print(f"🔧 COMMENT RÉSOUDRE : Consultez les logs distants : {url}")
    else:
        print(f"\n❌ [ERREUR] L'exécution s'est terminée avec le statut : {conclusion or 'unknown'}")
        print("❓ POURQUOI : Une erreur est survenue pendant l'exécution (problème de dépendance, erreur dans le code, ou défaillance de l'infrastructure).")
        print(f"🔧 COMMENT RÉSOUDRE : Consultez les logs distants pour voir la trace d'erreur complète : {url}")

def main():
    # Force standard output streams to use UTF-8 to prevent UnicodeEncodeError under Windows CMD/PowerShell
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(description="Cluster-CI Command Line Interface")
    parser.add_argument("command", nargs="?", default=None, choices=["list", "view", "cancel"],
                        help="Action to perform (default: submit a new shadow run)")
    parser.add_argument("run_id", nargs="?", default=None,
                        help="Target GHA run ID for 'view' or 'cancel'")

    args = parser.parse_args()

    check_dependencies()

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

        print(f"🛑 Cancelling run {run_id}...")
        subprocess.run(["gh", "run", "cancel", run_id])
        print(f"🧹 Deleting branch {branch}...")
        subprocess.run(["git", "push", "origin", "--delete", branch, "--quiet"])

    else:
        # Submit shadow run
        try:
            shadow_run()
        finally:
            cleanup()

if __name__ == "__main__":
    main()
