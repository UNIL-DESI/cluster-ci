import os
import json
import shutil
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

import time
import fcntl
import subprocess
import requests
import psutil
from pathlib import Path

def kill_host_dvc_viewer_processes():
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline') or []
                cmdline_str = " ".join(cmdline).lower()
                if "dvc-viewer" in cmdline_str or proc.info.get('name') == "dvc-viewer":
                    print(f"[Zombie GC] Killing host dvc-viewer process (PID: {proc.info['pid']})")
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except Exception as e:
        print(f"Error scanning for host dvc-viewer processes: {e}")

# Config
PANIC_THRESHOLD_GB = 50
PANIC_THRESHOLD_BYTES = PANIC_THRESHOLD_GB * 1024 * 1024 * 1024
DEFAULT_FREE_SPACE_THRESHOLD_GB = 100
FREE_SPACE_THRESHOLD_GB = int(os.environ.get("GC_FREE_SPACE_THRESHOLD_GB", DEFAULT_FREE_SPACE_THRESHOLD_GB))
FREE_SPACE_THRESHOLD_BYTES = FREE_SPACE_THRESHOLD_GB * 1024 * 1024 * 1024
REGISTRY_FILENAME = "registry.json"
ZOMBIE_REGISTRY_FILENAME = "zombie_registry.json"
LARGE_FILE_THRESHOLD_BYTES = 500 * 1024 * 1024
ZOMBIE_TIMEOUT_MINUTES = 10

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

def get_base_dir():
    # Assuming script is in src/runner/gc_orchestrator.py
    return Path(__file__).parent.parent.parent.resolve()

def get_repositories_dir():
    return get_base_dir() / "repositories"

def get_registry_path():
    return get_repositories_dir() / REGISTRY_FILENAME

def get_zombie_registry_path():
    return get_repositories_dir() / ZOMBIE_REGISTRY_FILENAME

def load_registry(f):
    try:
        f.seek(0)
        content = f.read()
        if not content:
            return {}
        return json.loads(content)
    except Exception as e:
        print(f"Error loading registry: {e}")
        return {}

def save_registry(f, registry):
    f.seek(0)
    f.truncate()
    json.dump(registry, f, indent=4)
    f.flush()
    os.fsync(f.fileno())

def get_dir_size(path):
    """Calculates directory size using 'du -sb' if available, otherwise os.walk."""
    try:
        # Use -s for summary and -b for bytes
        output = subprocess.check_output(["du", "-sb", str(path)], stderr=subprocess.DEVNULL)
        return int(output.split()[0])
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError, ValueError):
        # Fallback to os.walk
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    try:
                        total_size += os.path.getsize(fp)
                    except OSError:
                        pass
        return total_size

def validate_project_name(project_name):
    """Simple validation to ensure project_name is a relative path and doesn't escape repositories/."""
    path = Path(project_name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Invalid project name: {project_name}")

def update_running(project_name):
    validate_project_name(project_name)
    registry_path = get_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    with open(registry_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            registry = load_registry(f)
            registry[project_name] = registry.get(project_name, {})
            registry[project_name].update({
                "last_execution": time.time(),
                "status": "running"
            })
            save_registry(f, registry)
            print(f"Project {project_name} marked as running.")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def update_idle(project_name, project_path):
    validate_project_name(project_name)
    registry_path = get_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    if os.path.exists(project_path):
        size = get_dir_size(project_path)

    with open(registry_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            registry = load_registry(f)
            if project_name not in registry:
                registry[project_name] = {}

            registry[project_name].update({
                "status": "idle",
                "size_bytes": size,
                "last_execution": time.time()
            })
            save_registry(f, registry)
            print(f"Project {project_name} marked as idle. Size: {size} bytes.")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def mark_sync_status(project_name, status):
    validate_project_name(project_name)
    registry_path = get_registry_path()

    with open(registry_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            registry = load_registry(f)
            if project_name not in registry:
                registry[project_name] = {}
            registry[project_name]["sync_status"] = status
            save_registry(f, registry)
            print(f"Project {project_name} sync_status marked as {status}.")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def cleanup_level_1(project_path, project_name=None):
    """Level 1: Purge DVC history (keep only the last 2 commits)."""
    print(f"  [Level 1] Purging DVC history for {project_path}")
    try:
        # dvc gc -w (workspace) --keep-experiments --rev HEAD --rev HEAD~1
        # Note: we use -f (force) to avoid interactive prompt
        subprocess.run(
            [DVC_CMD, "gc", "-w", "-f", "--keep-experiments", "--rev", "HEAD", "--rev", "HEAD~1"],
            cwd=project_path,
            capture_output=True,
            text=True
        )
    except Exception as e:
        print(f"  Error in level 1 cleanup: {e}")

def cleanup_level_2(project_path, project_name=None):
    """Level 2: Delete large untracked files (> 500Mo) in working dirs, excluding .git and .dvc."""
    print(f"  [Level 2] Deleting large untracked files in {project_path}")
    try:
        for root, dirs, files in os.walk(project_path):
            # Skip .git and .dvc directories
            if ".git" in dirs:
                dirs.remove(".git")
            if ".dvc" in dirs:
                dirs.remove(".dvc")

            for file in files:
                file_path = Path(root) / file
                try:
                    if not file_path.is_symlink() and file_path.stat().st_size > LARGE_FILE_THRESHOLD_BYTES:
                        print(f"    Deleting large file: {file_path}")
                        file_path.unlink()
                except OSError:
                    pass
    except Exception as e:
        print(f"  Error in level 2 cleanup: {e}")

def cleanup_level_3(project_path, project_name=None):
    """Level 3: Delete virtual environment (Docker volume)."""
    if project_name is None:
        return
    volume_name = f"cluster-ci-home-{project_name.replace('/', '-')}"
    print(f"  [Level 3] Deleting Docker volume {volume_name} for {project_name}")
    try:
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume_name],
            capture_output=True,
            text=True
        )
    except Exception as e:
        print(f"  Error in level 3 cleanup: {e}")

def cleanup_level_4(project_path, project_name=None):
    """Level 4: Delete local DVC cache."""
    print(f"  [Level 4] Deleting DVC cache for {project_path}")
    cache_path = project_path / ".dvc" / "cache"
    if cache_path.exists():
        try:
            shutil.rmtree(cache_path)
        except Exception as e:
            print(f"  Error in level 4 cleanup: {e}")

def cleanup_level_5(project_path, project_name=None):
    """Level 5: Delete the entire project directory."""
    print(f"  [Level 5] Deleting entire directory {project_path}")
    try:
        shutil.rmtree(project_path)
    except Exception as e:
        print(f"  Error in level 5 cleanup: {e}")

def get_free_space():
    repo_dir = get_repositories_dir()
    if not repo_dir.exists():
        return 0
    usage = shutil.disk_usage(repo_dir)
    return usage.free

def run_gc():
    """Emergency GC: Purely destructive cleanup if space < 50GB."""
    repo_dir = get_repositories_dir()

    # Docker cleanup
    print("[Cluster Emergency] 🐳 Cleaning up Docker resources...")
    try:
        # Purge stopped containers, unused networks, and dangling images
        subprocess.run(["docker", "system", "prune", "-f"], capture_output=True)
    except Exception as e:
        print(f"  Error during Docker prune: {e}")

    if not repo_dir.exists():
        return

    free_space = get_free_space()
    print(f"[Cluster Emergency] Free space: {free_space / (1024**3):.2f} GB (Threshold: {PANIC_THRESHOLD_GB} GB)")

    if free_space < PANIC_THRESHOLD_BYTES:
        print(f"[Cluster Emergency] Critical space level! Starting emergency destructive cleanup...")
        registry_path = get_registry_path()
        if not registry_path.exists(): return

        with open(registry_path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                registry = load_registry(f)
                idle_projects = [(n, d) for n, d in registry.items() if d.get("status") == "idle"]
                idle_projects.sort(key=lambda x: x[1].get("last_execution", 0))

                for project_name, data in idle_projects:
                    if get_free_space() >= PANIC_THRESHOLD_BYTES: break
                    project_path = repo_dir / project_name
                    if not project_path.exists(): continue

                    print(f"[Cluster Emergency] Purging {project_name} immediately...")
                    cleanup_level_5(project_path, project_name)
                    data["status"] = "deleted"
                    data["size_bytes"] = 0

                save_registry(f, registry)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

def run_zombie_gc():
    """JIT Zombie Detection: Purge containers inactive for > 10 minutes."""
    repo_dir = get_repositories_dir()
    if not repo_dir.exists(): return

    zombie_registry_path = get_zombie_registry_path()
    zombie_registry_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Get all running containers related to cluster-ci
    try:
        res = subprocess.run(
            ["docker", "ps", "--filter", "name=cluster-job-", "--format", "{{.Names}}"],
            capture_output=True, text=True
        )
        containers = [c.strip() for c in res.stdout.strip().split("\n") if c.strip()]
    except Exception as e:
        print(f"Error listing containers: {e}")
        return

    if not containers:
        # Cleanup registry if no containers are running
        if zombie_registry_path.exists():
            try:
                os.remove(zombie_registry_path)
            except: pass
        # Clean up any leftover host-level dvc-viewer processes since no job containers are active
        kill_host_dvc_viewer_processes()
        return

    with open(zombie_registry_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            registry = load_registry(f)
            now = time.time()
            new_registry = {}

            for container_name in containers:
                has_activity = False

                # Extract Job ID and find log file
                # cluster-job-JOB_ID
                job_id = container_name.replace("cluster-job-", "")
                log_path = get_base_dir() / "job_logs" / f"{job_id}.log"

                # Dimension 1: Logs
                current_log_mtime = 0
                if log_path.exists():
                    current_log_mtime = log_path.stat().st_mtime

                # Dimension 2: CPU & Net
                current_cpu = 0.0
                current_net = ""
                try:
                    cmd = ["docker", "stats", "--no-stream", "--format", '{"cpu": "{{.CPUPerc}}", "net": "{{.NetIO}}"}', container_name]
                    stats_res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if stats_res.returncode == 0:
                        stats = json.loads(stats_res.stdout)
                        current_cpu = float(stats.get("cpu", "0%").replace("%", "").strip())
                        current_net = stats.get("net", "")
                except: pass

                # Dimension 3: GPU
                current_gpu = 0
                try:
                    gpu_res = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
                    if gpu_res.returncode == 0:
                        utils = [int(x) for x in gpu_res.stdout.strip().split("\n") if x.strip().isdigit()]
                        current_gpu = sum(utils)
                except: pass

                # Check against previous state
                prev_state = registry.get(container_name, {})
                last_activity = prev_state.get("last_activity", now)

                if current_cpu > 0.1 or current_gpu > 0:
                    has_activity = True

                if current_log_mtime > prev_state.get("log_mtime", 0):
                    has_activity = True

                if current_net and current_net != prev_state.get("net_io"):
                    has_activity = True

                if has_activity:
                    last_activity = now

                idle_duration = now - last_activity
                if idle_duration > (ZOMBIE_TIMEOUT_MINUTES * 60):
                    print(f"[Zombie GC] Killing zombie container {container_name} (Idle for {idle_duration/60:.1f}min)")
                    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
                    # Also kill viewer if present
                    subprocess.run(["docker", "rm", "-f", container_name.replace("cluster-job-", "cluster-viewer-")], capture_output=True)
                    # And kill leftover host processes
                    kill_host_dvc_viewer_processes()
                else:
                    new_registry[container_name] = {
                        "last_activity": last_activity,
                        "log_mtime": current_log_mtime,
                        "net_io": current_net
                    }

            save_registry(f, new_registry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def run_transfer_gc():
    """Maintenance GC: Lazy transfer to headnode if space < 100GB."""
    repo_dir = get_repositories_dir()
    free_space = get_free_space()
    print(f"[Cluster Maintenance] Free space: {free_space / (1024**3):.2f} GB (Threshold: {FREE_SPACE_THRESHOLD_GB} GB)")

    if free_space < FREE_SPACE_THRESHOLD_BYTES:
        print(f"[Cluster Maintenance] Space below threshold. Starting lazy transfer of old projects...")
        registry_path = get_registry_path()
        if not registry_path.exists(): return

        headnode_url = os.environ.get("HEADNODE_URL")
        if not headnode_url:
            print("❌ Error: HEADNODE_URL environment variable is missing.")
            sys.exit(1)

        # 1. Identify candidates (Locked briefly)
        candidates = []
        with open(registry_path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                registry = load_registry(f)
                idle_projects = [(n, d) for n, d in registry.items() if d.get("status") == "idle"]
                idle_projects.sort(key=lambda x: x[1].get("last_execution", 0))
                candidates = [n for n, d in idle_projects]
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        for project_name in candidates:
            if get_free_space() >= FREE_SPACE_THRESHOLD_BYTES: break
            project_path = repo_dir / project_name
            if not project_path.exists(): continue

            print(f"[Cluster Maintenance] Archiving old project: {project_name}...")

            # Check for DVC remote
            has_remote = False
            dvc_config = project_path / ".dvc" / "config"
            if dvc_config.exists():
                with open(dvc_config, "r") as cf:
                    if "remote =" in cf.read(): has_remote = True

            can_evict = True
            sync_status = "done"
            if has_remote:
                try:
                    resp = requests.get(f"{headnode_url}/check_space", timeout=5)
                    if resp.status_code == 200 and resp.json().get("sufficient"):
                        print(f"  Pushing {project_name} to headnode...")
                        push_res = subprocess.run([DVC_CMD, "push"], cwd=project_path, capture_output=True)
                        if push_res.returncode != 0:
                            print(f"  ❌ dvc push failed for {project_name}. Postponing eviction.")
                            sync_status = "pending"
                            can_evict = False
                    else:
                        print(f"  ⚠️ Headnode full or unreachable. Postponing eviction of {project_name}.")
                        sync_status = "pending"
                        can_evict = False
                except Exception as e:
                    print(f"  ⚠️ Error contacting headnode: {e}")
                    sync_status = "pending"
                    can_evict = False

            if can_evict:
                print(f"  Evicting {project_name} from worker.")
                cleanup_level_5(project_path, project_name)

            # 2. Commit changes to registry (Locked briefly)
            with open(registry_path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    registry = load_registry(f)
                    if project_name in registry:
                        registry[project_name]["sync_status"] = sync_status
                        if can_evict:
                            registry[project_name]["status"] = "deleted"
                            registry[project_name]["size_bytes"] = 0
                    save_registry(f, registry)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python gc_orchestrator.py <command> [args]")
        sys.exit(1)

    command = sys.argv[1]

    try:
        if command == "update-running":
            update_running(sys.argv[2])
        elif command == "update-idle":
            update_idle(sys.argv[2], sys.argv[3])
        elif command == "run-gc":
            run_gc()
        elif command == "run-transfer-gc":
            run_transfer_gc()
        elif command == "run-zombie-gc":
            run_zombie_gc()
        elif command == "get-free-space":
            print(get_free_space())
        elif command == "mark-sync-pending":
            mark_sync_status(sys.argv[2], "pending")
        elif command == "mark-sync-done":
            mark_sync_status(sys.argv[2], "done")
        else:
            print(f"Unknown command: {command}")
            sys.exit(1)
    except Exception as e:
        print(f"Error in {command}: {e}")
        sys.exit(1)
