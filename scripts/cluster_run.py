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

# Global variables for cleanup
RUN_ID = None
BRANCH = None
COMMIT_SHA = None
USER_INTERRUPTED = False
REPO_FULL_NAME = "UNIL-DESI/cluster-ci"

def check_dependencies():
    """Verify that gh and git are installed and accessible."""
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
        match = re.search(r"github\.com[:/]([^/]+/[^/.]+)(?:\.git)?", url)
        if match:
            REPO_FULL_NAME = match.group(1)
    except Exception:
        pass
    return REPO_FULL_NAME

def cleanup():
    """Remove draft branch and cancel active workflow run if user interrupted."""
    global RUN_ID, BRANCH, USER_INTERRUPTED
    if BRANCH:
        if RUN_ID and USER_INTERRUPTED:
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

def fetch_results(branch):
    print(f"📥 Fetching results from cluster branch origin/{branch}...")
    try:
        # 1. Fetch the latest commits on the draft branch
        subprocess.run(["git", "fetch", "origin", branch], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        # 2. Determine base ref for diff
        base_ref = COMMIT_SHA if COMMIT_SHA else "HEAD"
        
        # 3. Detect files modified by the execution on the cluster
        res_diff = subprocess.run(["git", "diff", base_ref, f"origin/{branch}", "--name-only"], capture_output=True, text=True)
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
                subprocess.run(["git", "checkout", f"origin/{branch}", "--"] + cluster_files, check=True)
                print("✅ Local workspace synchronized with cluster results successfully!")
            else:
                print("ℹ️ No changes in metrics, plots or dvc.lock detected on the cluster.")
        else:
            # Fallback to checkout dvc.lock directly if diff command fails
            subprocess.run(["git", "checkout", f"origin/{branch}", "--", "dvc.lock"], check=True)
            print("✅ Synchronized local dvc.lock (fallback).")
    except Exception as e:
        print(f"⚠️ Failed to auto-sync results from cluster: {e}")

def shadow_run():
    """Package current workspace changes, shadow commit, shadow push, and stream logs."""
    global RUN_ID, BRANCH, COMMIT_SHA, USER_INTERRUPTED
    check_gh_auth()
    user = get_current_user()
    BRANCH = f"cluster-draft/{user}"

    print(f"🏗️  Preparing shadow push for user: {user} (including untracked files)")

    fd, temp_index_path = tempfile.mkstemp()
    os.close(fd)
    
    commit_sha = None
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = temp_index_path

    try:
        subprocess.run(["git", "read-tree", "HEAD"], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "add", "--all"], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        res_tree = subprocess.run(["git", "write-tree"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        tree = res_tree.stdout.strip()
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
    print(f"📺 Watching logs for run {run_id} (Ctrl+C to cancel)...")
    
    try:
        subprocess.run(["gh", "run", "watch", str(run_id)])
    except KeyboardInterrupt:
        USER_INTERRUPTED = True
        print("\n🛑 Execution interrupted by user.")
        sys.exit(130)

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
        fetch_results(BRANCH)
    else:
        print(f"❌ Cluster-CI run finished with status: {conclusion or 'unknown'}")

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(description="Cluster-CI Command Line Interface")
    parser.add_argument("command", nargs="?", default=None, choices=["list", "view", "cancel", "sync"],
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
        
        subprocess.run(["gh", "run", "view", run_id, "--log"])

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

    elif args.command == "sync":
        check_gh_auth()
        user = get_current_user()
        branch = f"cluster-draft/{user}"
        fetch_results(branch)

    else:
        try:
            shadow_run()
        finally:
            cleanup()

if __name__ == "__main__":
    main()
