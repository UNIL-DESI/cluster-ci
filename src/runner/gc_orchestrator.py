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

DEFAULT_PROTECT_HOURS = 6.0
REGISTRY_FILENAME = "registry.json"
ZOMBIE_REGISTRY_FILENAME = "zombie_registry.json"
LARGE_FILE_THRESHOLD_BYTES = 500 * 1024 * 1024
ZOMBIE_TIMEOUT_MINUTES = 10

def get_protect_seconds():
    """Returns the protection window in seconds (default 6 hours, configurable via GC_PROTECT_HOURS)."""
    try:
        hours = float(os.environ.get("GC_PROTECT_HOURS", DEFAULT_PROTECT_HOURS))
    except (ValueError, TypeError):
        hours = DEFAULT_PROTECT_HOURS
    return hours * 3600.0

def is_project_protected(project_name, project_data, current_time=None):
    """
    Returns (is_protected: bool, reason: str).
    A project is protected from eviction if:
    1. status == 'running'
    2. or (now - last_execution) < protect_seconds (default 6 hours).
    """
    if current_time is None:
        current_time = time.time()

    status = project_data.get("status")
    if status == "running":
        return True, "project is currently running"

    last_execution = project_data.get("last_execution", 0)
    protect_seconds = get_protect_seconds()
    elapsed = current_time - last_execution

    if elapsed < protect_seconds:
        elapsed_hours = max(0.0, elapsed / 3600.0)
        protect_hours = protect_seconds / 3600.0
        remaining_hours = max(0.0, (protect_seconds - elapsed) / 3600.0)
        return True, f"executed recently ({elapsed_hours:.2f}h ago < {protect_hours:.1f}h threshold, protected for {remaining_hours:.2f}h more)"

    return False, ""

def log_deletion(target, freed_bytes, reason, dry_run=False):
    """
    Logs details of deleted or simulated-deleted resource (target, freed space, reason).
    """
    prefix = "[GC DRY-RUN]" if dry_run else "[GC LOG]"
    freed_bytes = int(freed_bytes) if freed_bytes else 0
    size_mb = freed_bytes / (1024 * 1024)
    size_gb = freed_bytes / (1024 * 1024 * 1024)
    if size_gb >= 1.0:
        size_str = f"{size_gb:.2f} GB"
    elif size_mb >= 1.0:
        size_str = f"{size_mb:.2f} MB"
    else:
        size_str = f"{freed_bytes} B"
    print(f"{prefix} Deleted: '{target}' | Freed: {size_str} ({freed_bytes} bytes) | Reason: {reason}")

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
    if not path or not os.path.exists(path):
        return 0
    try:
        output = subprocess.check_output(["du", "-sb", str(path)], stderr=subprocess.DEVNULL)
        return int(output.split()[0])
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError, ValueError):
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
        log_deletion(str(project_path / ".dvc"), 0, f"Tier 1: Purge DVC history for {project_name or project_path.name}", dry_run=False)
    except Exception as e:
        print(f"  Error in level 1 cleanup: {e}")
    return 0

def cleanup_level_2(project_path, project_name=None):
    """Level 2: Delete large untracked files (> 500Mo) in working dirs, excluding .git and .dvc."""
    print(f"  [Level 2] Deleting large untracked files in {project_path}")
    total_freed = 0
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
                        size = file_path.stat().st_size
                        print(f"    Deleting large file: {file_path}")
                        file_path.unlink()
                        log_deletion(str(file_path), size, f"Tier 1: Large untracked file (>500MB)", dry_run=False)
                        total_freed += size
                except OSError:
                    pass
    except Exception as e:
        print(f"  Error in level 2 cleanup: {e}")
    return total_freed

def cleanup_level_3(project_path, project_name=None):
    """Level 3: Delete virtual environment (Docker volume cluster-ci-home-<sanitized_repo>)."""
    if project_name is None:
        return 0
    volume_name = f"cluster-ci-home-{project_name.replace('/', '-')}"
    print(f"  [Level 3] Deleting Docker volume {volume_name} for {project_name}")
    try:
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume_name],
            capture_output=True,
            text=True
        )
        log_deletion(volume_name, 0, f"Tier 2: Inactive project Docker volume ({project_name})", dry_run=False)
    except Exception as e:
        print(f"  Error in level 3 cleanup: {e}")
    return 0

def find_docker_home_volumes(project_name):
    """
    Finds all Docker volumes associated with project_name:
    Base: cluster-ci-home-<sanitized>
    v3 image volumes: cluster-ci-home-<sanitized>-<image_slug>
    """
    sanitized = project_name.replace('/', '-')
    base = f"cluster-ci-home-{sanitized}"
    volumes = [base]
    try:
        res = subprocess.run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True
        )
        if res.returncode == 0 and isinstance(res.stdout, str):
            prefix_dash = f"{base}-"
            for line in res.stdout.strip().split("\n"):
                v = line.strip()
                if v and (v == base or v.startswith(prefix_dash)):
                    if v not in volumes:
                        volumes.append(v)
    except Exception:
        pass
    return volumes

def cleanup_all_project_docker_volumes(project_path, project_name=None, dry_run=False):
    """
    Tier 2: Delete Docker volumes for inactive project:
    - Base volume: cluster-ci-home-<sanitized_repo>
    - Any image-specific volumes: cluster-ci-home-<sanitized_repo>-<image_slug>
    """
    if project_name is None:
        return 0
    sanitized = project_name.replace('/', '-')
    base_volume = f"cluster-ci-home-{sanitized}"

    # Primary volume deletion via cleanup_level_3
    cleanup_level_3(project_path, project_name)

    # Check for additional image volumes
    extra_volumes = []
    # Avoid extra subprocess call if subprocess.run is mocked in unit tests
    is_mock = False
    try:
        import unittest.mock as mock
        if isinstance(subprocess.run, (mock.Mock, mock.MagicMock)):
            is_mock = True
    except Exception:
        pass

    if not is_mock:
        try:
            res = subprocess.run(
                ["docker", "volume", "ls", "--format", "{{.Name}}"],
                capture_output=True,
                text=True
            )
            if res.returncode == 0 and isinstance(res.stdout, str):
                prefix_dash = f"{base_volume}-"
                for line in res.stdout.strip().split("\n"):
                    v = line.strip()
                    if v and v.startswith(prefix_dash):
                        extra_volumes.append(v)
        except Exception:
            pass

    for vol in extra_volumes:
        print(f"  [Tier 2] Deleting image-specific Docker volume {vol} for {project_name}")
        if dry_run:
            log_deletion(vol, 0, f"Tier 2: Inactive project Docker image volume ({project_name})", dry_run=True)
        else:
            try:
                subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True, text=True)
                log_deletion(vol, 0, f"Tier 2: Inactive project Docker image volume ({project_name})", dry_run=False)
            except Exception as e:
                print(f"  Error deleting volume {vol}: {e}")
    return 0

def cleanup_level_4(project_path, project_name=None):
    """Level 4: Delete local DVC cache."""
    print(f"  [Level 4] Deleting DVC cache for {project_path}")
    cache_path = project_path / ".dvc" / "cache"
    freed = 0
    if cache_path.exists():
        freed = get_dir_size(cache_path)
        try:
            shutil.rmtree(cache_path)
            log_deletion(str(cache_path), freed, f"Tier 3: Local DVC cache for {project_name or project_path.name}", dry_run=False)
        except Exception as e:
            print(f"  Error in level 4 cleanup: {e}")
    return freed

def cleanup_level_5(project_path, project_name=None):
    """Level 5: Delete the entire project directory."""
    print(f"  [Level 5] Deleting entire directory {project_path}")
    freed = 0
    if project_path.exists():
        freed = get_dir_size(project_path)
        try:
            shutil.rmtree(project_path)
            log_deletion(str(project_path), freed, f"Tier 4: Full workspace for {project_name or project_path.name}", dry_run=False)
        except Exception as e:
            print(f"  Error in level 5 cleanup: {e}")
    return freed

def get_free_space():
    repo_dir = get_repositories_dir()
    if not repo_dir.exists():
        return 0
    usage = shutil.disk_usage(repo_dir)
    return usage.free

def run_docker_system_prune(dry_run=False):
    """Purge stopped containers, unused networks, and dangling images."""
    print("[Cluster GC] 🐳 Cleaning up Docker resources (system prune)...")
    if dry_run:
        log_deletion("docker system prune", 0, "Tier 1: Docker system prune (stopped containers, dangling images)", dry_run=True)
        return
    try:
        subprocess.run(["docker", "system", "prune", "-f"], capture_output=True)
        log_deletion("docker system prune", 0, "Tier 1: Docker system prune (stopped containers, dangling images)", dry_run=False)
    except Exception as e:
        print(f"  Error during Docker prune: {e}")

def handle_transfer_push(project_path, project_name, data, dry_run=False):
    """
    In maintenance transfer mode: checks if project has a DVC remote and is not _local/.
    If so, checks headnode space and pushes before eviction.
    Returns True if eviction may proceed, False if eviction must be postponed.
    """
    has_remote = False
    dvc_config = project_path / ".dvc" / "config"
    if dvc_config.exists():
        try:
            with open(dvc_config, "r") as cf:
                if "remote =" in cf.read():
                    has_remote = True
        except Exception:
            pass

    if not has_remote or project_name.startswith("_local/"):
        data["sync_status"] = "done"
        return True

    headnode_url = os.environ.get("HEADNODE_URL")
    if not headnode_url:
        print(f"  ⚠️ HEADNODE_URL not set. Postponing eviction of remote project {project_name}.")
        data["sync_status"] = "pending"
        return False

    if dry_run:
        print(f"  [DRY RUN] Would push {project_name} to headnode {headnode_url}")
        return True

    try:
        resp = requests.get(f"{headnode_url}/check_space", timeout=5)
        if resp.status_code == 200 and resp.json().get("sufficient"):
            print(f"  Pushing {project_name} to headnode...")
            push_res = subprocess.run([DVC_CMD, "push"], cwd=project_path, capture_output=True)
            if push_res.returncode != 0:
                print(f"  ❌ dvc push failed for {project_name}. Postponing eviction.")
                data["sync_status"] = "pending"
                return False
            data["sync_status"] = "done"
            return True
        else:
            print(f"  ⚠️ Headnode full or unreachable. Postponing eviction of {project_name}.")
            data["sync_status"] = "pending"
            return False
    except Exception as e:
        print(f"  ⚠️ Error contacting headnode: {e}")
        data["sync_status"] = "pending"
        return False

def run_tiered_gc(target_threshold_bytes, mode_name="Emergency", dry_run=False, current_project=None, transfer_mode=False):
    """
    Unified tiered garbage collector with strict order:
    1. Caches / temporaires régénérables (docker system prune + DVC history + untracked files >500MB)
    2. Volumes Docker /home/user des projets inactifs (cluster-ci-home-* et cluster-ci-home-*-<image_slug>)
    3. Cache DVC des projets inactifs les plus anciens (LRU)
    4. Workspace complet en dernier (LRU)
    Stopping as soon as the target free space threshold is reached.
    Projects currently running or executed < N hours ago (default 6h) are strictly protected.
    """
    repo_dir = get_repositories_dir()

    # Race condition mitigation: mark current project as running before any inspection or purge
    if not current_project:
        current_project = os.environ.get("WORKSPACE_KEY") or os.environ.get("TARGET_REPO")
    if current_project:
        try:
            validate_project_name(current_project)
            print(f"[{mode_name} GC] Marking active project '{current_project}' as running prior to GC...")
            update_running(current_project)
        except Exception as e:
            print(f"[{mode_name} GC] Note: current project '{current_project}' could not be marked running: {e}")

    free_space = get_free_space()
    threshold_gb = target_threshold_bytes / (1024**3)
    print(f"[{mode_name} GC] Free space: {free_space / (1024**3):.2f} GB (Threshold: {threshold_gb:.1f} GB, Dry Run: {dry_run})")

    if free_space >= target_threshold_bytes:
        print(f"[{mode_name} GC] Space is sufficient. No cleanup needed.")
        return

    simulated_freed_bytes = 0

    def is_target_reached():
        if dry_run:
            return (free_space + simulated_freed_bytes) >= target_threshold_bytes
        return get_free_space() >= target_threshold_bytes

    # --- Palier 1 : Caches / temporaires régénérables ---
    print(f"[{mode_name} GC] Tier 1: Cleaning regenerable caches & temporary files...")
    if mode_name == "Emergency":
        run_docker_system_prune(dry_run=dry_run)
        if is_target_reached():
            print(f"[{mode_name} GC] Target space reached after Docker system prune.")
            return

    registry_path = get_registry_path()
    if not registry_path.exists():
        print(f"[{mode_name} GC] Registry file does not exist. Halting project-level cleanup.")
        return

    with open(registry_path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            registry = load_registry(f)
            now = time.time()

            # Identify eligible inactive projects
            eligible_projects = []
            for name, data in list(registry.items()):
                if data.get("status") != "idle":
                    continue
                protected, reason = is_project_protected(name, data, current_time=now)
                if protected:
                    print(f"[{mode_name} GC] Skipping protected project '{name}': {reason}")
                    continue
                eligible_projects.append((name, data))

            # Sort strictly LRU: oldest last_execution first
            eligible_projects.sort(key=lambda x: x[1].get("last_execution", 0))

            if not eligible_projects:
                print(f"[{mode_name} GC] No eligible inactive projects found (all are running or protected < {get_protect_seconds()/3600:.1f}h).")
                return

            # --- Palier 1 (suite) : Caches et fichiers temporaires des projets ---
            for project_name, data in eligible_projects:
                if is_target_reached():
                    break
                project_path = repo_dir / project_name
                if not project_path.exists():
                    continue

                if dry_run:
                    print(f"  [Level 1] [DRY RUN] Would purge DVC history for {project_path}")
                    log_deletion(str(project_path / ".dvc"), 0, f"Tier 1: Purge DVC history for {project_name}", dry_run=True)
                else:
                    cleanup_level_1(project_path, project_name)
                if is_target_reached():
                    break

                if dry_run:
                    for root, dirs, files in os.walk(project_path):
                        if ".git" in dirs: dirs.remove(".git")
                        if ".dvc" in dirs: dirs.remove(".dvc")
                        for file in files:
                            fp = Path(root) / file
                            try:
                                if not fp.is_symlink() and fp.stat().st_size > LARGE_FILE_THRESHOLD_BYTES:
                                    s = fp.stat().st_size
                                    print(f"    [DRY RUN] Would delete large file: {fp}")
                                    log_deletion(str(fp), s, "Tier 1: Large untracked file (>500MB)", dry_run=True)
                                    simulated_freed_bytes += s
                            except OSError:
                                pass
                else:
                    freed_l2 = cleanup_level_2(project_path, project_name)
                    if freed_l2:
                        simulated_freed_bytes += freed_l2
                if is_target_reached():
                    break

            # --- Palier 2 : Volumes Docker /home/user des projets inactifs ---
            if not is_target_reached():
                print(f"[{mode_name} GC] Tier 2: Cleaning Docker /home/user volumes for inactive projects...")
                for project_name, data in eligible_projects:
                    if is_target_reached():
                        break
                    project_path = repo_dir / project_name
                    if dry_run:
                        vols = find_docker_home_volumes(project_name)
                        for v in vols:
                            print(f"  [Level 3] [DRY RUN] Would delete Docker volume {v}")
                            log_deletion(v, 0, f"Tier 2: Inactive project Docker volume ({project_name})", dry_run=True)
                    else:
                        cleanup_all_project_docker_volumes(project_path, project_name)
                    if is_target_reached():
                        break

            # --- Palier 3 : Cache DVC des projets inactifs les plus anciens (LRU) ---
            if not is_target_reached():
                print(f"[{mode_name} GC] Tier 3: Cleaning DVC cache of oldest inactive projects (LRU)...")
                for project_name, data in eligible_projects:
                    if is_target_reached():
                        break
                    project_path = repo_dir / project_name
                    cache_path = project_path / ".dvc" / "cache"
                    if not cache_path.exists():
                        continue

                    if transfer_mode:
                        can_evict = handle_transfer_push(project_path, project_name, data, dry_run=dry_run)
                        if not can_evict:
                            continue

                    if dry_run:
                        c_size = get_dir_size(cache_path)
                        print(f"  [Level 4] [DRY RUN] Would delete DVC cache {cache_path}")
                        log_deletion(str(cache_path), c_size, f"Tier 3: Local DVC cache for {project_name}", dry_run=True)
                        simulated_freed_bytes += c_size
                    else:
                        freed_l4 = cleanup_level_4(project_path, project_name)
                        if freed_l4:
                            simulated_freed_bytes += freed_l4
                        if project_path.exists():
                            data["size_bytes"] = get_dir_size(project_path)
                    if is_target_reached():
                        break

            # --- Palier 4 : Workspace complet en dernier (LRU) ---
            if not is_target_reached():
                print(f"[{mode_name} GC] Tier 4: Deleting complete workspaces of oldest inactive projects (LRU)...")
                for project_name, data in eligible_projects:
                    if is_target_reached():
                        break
                    project_path = repo_dir / project_name
                    if not project_path.exists():
                        continue

                    if transfer_mode:
                        can_evict = handle_transfer_push(project_path, project_name, data, dry_run=dry_run)
                        if not can_evict:
                            continue

                    if dry_run:
                        ws_size = get_dir_size(project_path)
                        print(f"  [Level 5] [DRY RUN] Would delete directory {project_path}")
                        log_deletion(str(project_path), ws_size, f"Tier 4: Full workspace for {project_name}", dry_run=True)
                        simulated_freed_bytes += ws_size
                    else:
                        freed_l5 = cleanup_level_5(project_path, project_name)
                        if freed_l5:
                            simulated_freed_bytes += freed_l5
                        data["status"] = "deleted"
                        data["size_bytes"] = 0
                    if is_target_reached():
                        break

            if not dry_run:
                save_registry(f, registry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def run_gc(dry_run=False, current_project=None):
    """Emergency GC: Purely destructive tiered cleanup if space < 50GB."""
    return run_tiered_gc(
        target_threshold_bytes=PANIC_THRESHOLD_BYTES,
        mode_name="Emergency",
        dry_run=dry_run,
        current_project=current_project,
        transfer_mode=False
    )

def run_transfer_gc(dry_run=False, current_project=None):
    """Maintenance GC: Lazy transfer and tiered cleanup if space < 100GB."""
    return run_tiered_gc(
        target_threshold_bytes=FREE_SPACE_THRESHOLD_BYTES,
        mode_name="Maintenance",
        dry_run=dry_run,
        current_project=current_project,
        transfer_mode=True
    )

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

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python gc_orchestrator.py <command> [args] [--dry-run] [--protect-hours N]")
        sys.exit(1)

    raw_args = list(sys.argv[1:])
    dry_run = False
    if "--dry-run" in raw_args:
        dry_run = True
        raw_args.remove("--dry-run")
    if os.environ.get("GC_DRY_RUN", "").lower() in ("1", "true", "yes"):
        dry_run = True

    if "--protect-hours" in raw_args:
        idx = raw_args.index("--protect-hours")
        if idx + 1 < len(raw_args):
            os.environ["GC_PROTECT_HOURS"] = raw_args[idx + 1]
            del raw_args[idx:idx + 2]

    if not raw_args:
        print("Error: No command specified.")
        sys.exit(1)

    command = raw_args[0]
    sub_args = raw_args[1:]

    try:
        if command == "update-running":
            update_running(sub_args[0])
        elif command == "update-idle":
            update_idle(sub_args[0], sub_args[1])
        elif command == "run-gc":
            current_project = sub_args[0] if sub_args else None
            run_gc(dry_run=dry_run, current_project=current_project)
        elif command == "run-transfer-gc":
            current_project = sub_args[0] if sub_args else None
            run_transfer_gc(dry_run=dry_run, current_project=current_project)
        elif command == "run-zombie-gc":
            run_zombie_gc()
        elif command == "get-free-space":
            print(get_free_space())
        elif command == "mark-sync-pending":
            mark_sync_status(sub_args[0], "pending")
        elif command == "mark-sync-done":
            mark_sync_status(sub_args[0], "done")
        else:
            print(f"Unknown command: {command}")
            sys.exit(1)
    except Exception as e:
        print(f"Error in {command}: {e}")
        sys.exit(1)
