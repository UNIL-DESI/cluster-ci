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
    
    # 1. Connect to live terminal via piping
    if commit_sha:
        print("🔍 Connecting to live terminal session via piping...")
        print("⚡ Capturing real-time logs from runner (streaming to your terminal)...")
        print("==========================================================================")
        
        last_gh_log_time = 0
        try:
            while True:
                # Prior check of GHA run status
                status = "queued"
                conclusion = None
                try:
                    res = subprocess.run(["gh", "run", "view", str(run_id), "--json", "status,conclusion"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                    if res.returncode == 0:
                        info = json.loads(res.stdout)
                        status = info.get("status")
                        conclusion = info.get("conclusion")
                        if status == "completed" or conclusion:
                            if conclusion == "success":
                                print("✅ Cluster-CI run completed successfully!")
                            else:
                                print(f"❌ Cluster-CI run finished with status: {conclusion}")
                            break
                except Exception:
                    pass

                # If the job is active but ppng.io is not receiving data (e.g. still in queue)
                # Fetch recent logs from GitHub every 15 seconds to show queue/startup status dynamically on-place
                if time.time() - last_gh_log_time > 15:
                    display_clean_queue_status(run_id)
                    last_gh_log_time = time.time()

                # Start curl process to stream from ppng.io
                proc = subprocess.Popen(
                    ["curl", "-s", "-N", f"https://ppng.io/cluster-ci-log-{commit_sha}"],
                    stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
                )
                
                stop_event = threading.Event()
                
                def monitor_run_status():
                    while not stop_event.is_set() and proc.poll() is None:
                        time.sleep(5)
                        try:
                            res = subprocess.run(["gh", "run", "view", str(run_id), "--json", "status,conclusion"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                            if res.returncode == 0:
                                status_info = json.loads(res.stdout)
                                status = status_info.get("status")
                                conclusion = status_info.get("conclusion")
                                if status == "completed" or conclusion:
                                    proc.terminate()
                                    break
                        except Exception:
                            pass

                monitor_thread = threading.Thread(target=monitor_run_status, daemon=True)
                monitor_thread.start()

                # Read line by line and print cleanly
                for line in proc.stdout:
                    print_line(line.rstrip('\r\n'), force=True)

                # Wait for curl to finish
                proc.wait()
                stop_event.set()

                # Check GHA status again
                try:
                    res = subprocess.run(["gh", "run", "view", str(run_id), "--json", "status,conclusion"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                    if res.returncode == 0:
                        info = json.loads(res.stdout)
                        status = info.get("status")
                        conclusion = info.get("conclusion")
                        if status == "completed" or conclusion:
                            if conclusion == "success":
                                print("✅ Cluster-CI run completed successfully!")
                            else:
                                print(f"❌ Cluster-CI run finished with status: {conclusion}")
                            break
                except Exception:
                    pass

                # If the run is still active but curl stopped, wait and reconnect
                print("\n🔄 Connection to piping lost or waiting for runner. Reconnecting in 3s...")
                time.sleep(3)

        except KeyboardInterrupt:
            try:
                proc.terminate()
            except Exception:
                pass
            raise
            
        print("==========================================================================")
        return 0

    # 3. Fallback: API Polling & Consolidated Logs Dump
    print("📺 Live terminal not available. Waiting for GHA completion to fetch logs...")
    spin_idx = 0
    spin_chars = ["/", "-", "\\", "|"]
    
    while True:
        try:
            res = subprocess.run(["gh", "run", "view", str(run_id), "--json", "status,conclusion"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            info = json.loads(res.stdout) if res.returncode == 0 else {"status": "queued", "conclusion": None}
        except Exception:
            info = {"status": "queued", "conclusion": None}
            
        status = info.get("status", "queued")
        conclusion = info.get("conclusion")
        
        char = spin_chars[spin_idx]
        spin_idx = (spin_idx + 1) % 4
        
        if status == "completed" or conclusion:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            print("📥 Job completed. Fetching consolidated logs...")
            print("==========================================================================")
            try:
                log_res = subprocess.run(["gh", "run", "view", str(run_id), "--log"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                if log_res.returncode == 0:
                    for line in log_res.stdout.splitlines():
                        # Parse lines that have: "timestamp \t step_name \t log_content"
                        parts = line.split("\t")
                        if len(parts) >= 3:
                            step = parts[1]
                            content = parts[2]
                            # Clean GHA noise
                            content = content.replace("\ufeff", "")
                            # Strip GHA timestamp prefixes if present
                            content = re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ", "", content)
                            content = content.replace("##[group]", "▶️  ").replace("##[endgroup]", "")
                            if content.strip():
                                print(f"\033[90m[{step}]\033[0m {content}")
            except Exception as e:
                print(f"Error fetching GHA logs: {e}")
                
            print("==========================================================================")
            if conclusion == "success":
                print("✅ Cluster-CI run completed successfully!")
                return 0
            elif conclusion == "cancelled":
                print("⚠️  Cluster-CI run was cancelled.")
                return 1
            else:
                print(f"❌ Cluster-CI run finished with status: {conclusion or 'failed'}")
                return 1
                
        if status == "queued":
            sys.stdout.write(f"\r⏳ Waiting in GitHub Actions queue [{char}]...")
        else:
            sys.stdout.write(f"\r⏱️  Job in progress [{char}] (logs will appear on completion)...")
        sys.stdout.flush()
        time.sleep(3)

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

def shadow_run(background=False):
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

    if background:
        print("✅ Run submitted in background. You can watch it with: cluster-run list")
        return

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
    for _ in range(5):
        try:
            res = subprocess.run(["gh", "run", "view", str(run_id), "--json", "conclusion"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode == 0:
                conclusion = json.loads(res.stdout).get("conclusion", "unknown")
                if conclusion and conclusion != "null":
                    break
        except Exception:
            pass
        time.sleep(1)

    if conclusion == "success":
        print("✅ Cluster-CI run completed successfully.")
        print("📥 Fetching updated dvc.lock and metrics from cluster...")
        try:
            # 1. Fetch the latest commits on the draft branch
            subprocess.run(["git", "fetch", "origin", BRANCH], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # 2. Detect files changed by the cluster execution (excluding .cluster-ci-commit or other temporary markers)
            res_diff = subprocess.run(["git", "diff", "HEAD", f"origin/{BRANCH}", "--name-only"], capture_output=True, text=True)
            if res_diff.returncode == 0:
                files_to_sync = []
                for line in res_diff.stdout.splitlines():
                    name = line.strip()
                    if name and not name.startswith(".cluster-ci"):
                        files_to_sync.append(name)
                
                if files_to_sync:
                    print(f"📂 Syncing modified files from cluster: {', '.join(files_to_sync)}")
                    # Checkout these files from the remote draft branch
                    subprocess.run(["git", "checkout", f"origin/{BRANCH}", "--"] + files_to_sync, check=True)
                    print("✅ Local workspace synchronized with cluster artifacts.")
                else:
                    print("ℹ️ No changes detected in the cluster execution.")
            else:
                # Fallback to checkout dvc.lock directly if diff command fails
                subprocess.run(["git", "checkout", f"origin/{BRANCH}", "--", "dvc.lock"], check=True)
                print("✅ Synchronized local dvc.lock.")
        except Exception as e:
            print(f"⚠️ Failed to auto-sync dvc.lock/metrics from cluster: {e}")
    else:
        print(f"❌ Cluster-CI run finished with status: {conclusion or 'unknown'}")

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
    parser.add_argument("-b", "--background", action="store_true",
                        help="Submit the run and exit without watching logs")

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
                if status in ("in_progress", "queued") and head_sha:
                    print(f"📺 Run {run_id} is {status}. Resuming live stream...")
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
            shadow_run(background=args.background)
        finally:
            if not args.background:
                cleanup()

if __name__ == "__main__":
    main()
