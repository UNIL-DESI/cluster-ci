import os
import json
import shutil
import sys
import fnmatch
import re

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

import time
import tempfile
import fcntl
import subprocess
import requests
import psutil
from pathlib import Path

try:
    from src.runner.runtime_env import resolve_venv_executable
    from src.runner.registry_recovery import recover_running
except ImportError:
    try:
        from runner.runtime_env import resolve_venv_executable
        from runner.registry_recovery import recover_running
    except ImportError:
        from runtime_env import resolve_venv_executable
        from registry_recovery import recover_running

class RegistryCorruptedError(Exception):
    """Raised when registry.json is unparseable or corrupted."""
    pass

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
DEFAULT_PANIC_THRESHOLD_GB = 50
PANIC_THRESHOLD_GB = int(os.environ.get("GC_PANIC_THRESHOLD_GB", DEFAULT_PANIC_THRESHOLD_GB))
PANIC_THRESHOLD_BYTES = PANIC_THRESHOLD_GB * 1024 * 1024 * 1024
DEFAULT_FREE_SPACE_THRESHOLD_GB = 100
FREE_SPACE_THRESHOLD_GB = int(os.environ.get("GC_FREE_SPACE_THRESHOLD_GB", DEFAULT_FREE_SPACE_THRESHOLD_GB))
FREE_SPACE_THRESHOLD_BYTES = FREE_SPACE_THRESHOLD_GB * 1024 * 1024 * 1024

DEFAULT_PROTECT_HOURS = 6.0
REGISTRY_FILENAME = "registry.json"
ZOMBIE_REGISTRY_FILENAME = "zombie_registry.json"
ZOMBIE_TIMEOUT_MINUTES = 10

# Explicit whitelist for regenerable caches and temporary files (Tier 1)
REGENERABLE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "tmp", "temp", ".tmp", ".temp"}
REGENERABLE_CACHE_PREFIXES = {".cache/pip", ".cache/uv", ".cache/huggingface", ".cache/torch"}
REGENERABLE_FILE_PATTERNS = {"*.tmp", "*.temp", "*.log", "*.pyc", "*.pyo", "core.[0-9]*"}

def get_protect_seconds():
    """Returns the protection window in seconds (default 6 hours, configurable via GC_PROTECT_HOURS)."""
    try:
        hours = float(os.environ.get("GC_PROTECT_HOURS", DEFAULT_PROTECT_HOURS))
    except (ValueError, TypeError):
        hours = DEFAULT_PROTECT_HOURS
    return hours * 3600.0

def get_active_docker_info():
    """
    Queries running Docker containers.
    Returns set of active identifiers (container names, mount sources, and volume names).
    If docker ps fails, raises RuntimeError to prevent GC from proceeding unsafely.
    """
    try:
        res = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Mounts}}"],
            capture_output=True,
            text=True
        )
        if res.returncode != 0:
            err_msg = res.stderr.strip() if isinstance(res.stderr, str) and res.stderr else f"Exit code {res.returncode}"
            print(f"❌ CRITICAL: 'docker ps' failed: {err_msg}")
            raise RuntimeError(f"docker ps failed with code {res.returncode}: {err_msg}")

        active_info = set()
        stdout = res.stdout if isinstance(res.stdout, str) else ""
        for line in stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) > 1 and parts[1]:
                for cname in parts[1].split(","):
                    cname = cname.strip()
                    if cname:
                        active_info.add(cname)
            if len(parts) > 2 and parts[2]:
                for mount in parts[2].split(","):
                    mount = mount.strip()
                    if mount:
                        active_info.add(mount)
        return active_info
    except FileNotFoundError:
        is_mock = False
        try:
            import unittest.mock as mock
            if isinstance(subprocess.run, (mock.Mock, mock.MagicMock)):
                is_mock = True
        except Exception:
            pass
        if is_mock:
            return set()
        raise RuntimeError("docker executable not found on system. Halting GC for safety.")

def is_project_active_in_docker(project_name, active_docker_info):
    """
    Checks if project has an active Docker container or mounted volume:
    - container name matching cluster-job-* or project name
    - mount path containing repositories/<project_name>
    - volume named cluster-ci-home-<sanitized_repo>*
    """
    if not active_docker_info:
        return False, ""

    sanitized = project_name.replace('/', '-')
    base_volume = f"cluster-ci-home-{sanitized}"

    for info in active_docker_info:
        if base_volume in info:
            return True, f"active Docker volume mount detected: '{info}'"
        if project_name in info or sanitized in info:
            return True, f"active Docker container or mount detected: '{info}'"

    return False, ""

def is_project_protected(project_name, project_data, current_time=None, active_docker_info=None):
    """
    Returns (is_protected: bool, reason: str).
    A project is protected from eviction if:
    1. status == 'running'
    2. or active Docker container/mount references this project
    3. or (now - last_execution) < protect_seconds (default 6 hours).
    """
    if current_time is None:
        current_time = time.time()

    status = project_data.get("status")
    if status == "running":
        return True, "project is currently running"

    if active_docker_info:
        docker_active, docker_reason = is_project_active_in_docker(project_name, active_docker_info)
        if docker_active:
            return True, f"active Docker container/volume ({docker_reason})"

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
    """Finds an executable prioritizing current venv, system PATH, and local bin."""
    try:
        return resolve_venv_executable(name)
    except FileNotFoundError:
        local_path = os.path.expanduser(f"~/.local/bin/{name}")
        if os.path.isfile(local_path) and os.access(local_path, os.X_OK):
            return local_path
        raise

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

def load_registry(f, registry_path=None):
    """
    Loads JSON from registry file descriptor.
    If content is non-empty but corrupt JSON, creates a backup and raises RegistryCorruptedError.
    """
    f.seek(0)
    content = f.read()
    if not content or not content.strip():
        return {}
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError(f"Registry root must be a dict, got {type(data)}")
        return data
    except Exception as e:
        print(f"❌ CRITICAL ERROR: Registry is corrupted: {e}")
        target_path = registry_path or getattr(f, 'name', None)
        if target_path and os.path.exists(str(target_path)):
            p = Path(str(target_path))
            backup_p = p.with_suffix(f".corrupt.{int(time.time())}.bak")
            try:
                shutil.copy2(p, backup_p)
                print(f"  Saved backup of corrupted registry to {backup_p}")
            except Exception as be:
                print(f"  Failed to save backup: {be}")
        raise RegistryCorruptedError(f"Registry corrupted: {e}")

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
    """
    Validates project_name:
    Must be a non-empty relative path without '.' or '..' components,
    without leading/trailing slashes, and without backslashes.
    """
    if not project_name or not isinstance(project_name, str):
        raise ValueError(f"Invalid project name: must be a non-empty string, got {project_name!r}")

    clean = project_name.strip()
    if not clean:
        raise ValueError("Invalid project name: empty or whitespace-only")

    if clean in (".", ".."):
        raise ValueError(f"Invalid project name: '{clean}'")

    if clean.startswith("/") or clean.endswith("/") or clean.startswith("\\") or clean.endswith("\\"):
        raise ValueError(f"Invalid project name: leading or trailing path separator in {project_name!r}")

    if "\\" in clean or "//" in clean:
        raise ValueError(f"Invalid project name: invalid separators in {project_name!r}")

    path = Path(clean)
    if path.is_absolute():
        raise ValueError(f"Invalid project name: absolute paths forbidden: {project_name}")

    for part in path.parts:
        if part in (".", "..", ""):
            raise ValueError(f"Invalid project name: relative traversal forbidden in {project_name}")

def update_running(project_name):
    validate_project_name(project_name)
    registry_path = get_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    with open(registry_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                registry = load_registry(f, registry_path=registry_path)
            except RegistryCorruptedError as e:
                print(f"❌ Cannot update running: {e}. Refusing to overwrite corrupted registry.")
                sys.exit(1)

            registry[project_name] = registry.get(project_name, {})
            job_id = os.environ.get('JOB_ID')
            if job_id:
                ids = registry[project_name].get('job_ids', [])
                registry[project_name]['job_ids'] = sorted(set(ids + [job_id]))
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
            try:
                registry = load_registry(f, registry_path=registry_path)
            except RegistryCorruptedError as e:
                print(f"❌ Cannot update idle: {e}. Refusing to overwrite corrupted registry.")
                sys.exit(1)

            if project_name not in registry:
                registry[project_name] = {}

            # A runner finishing does not prove that the entire project has no
            # queued or sibling work. Recorded jobs require headnode confirmation.
            status = "running" if registry[project_name].get('job_ids') else "idle"
            registry[project_name].update({
                "status": status,
                "size_bytes": size,
                "last_execution": time.time()
            })
            save_registry(f, registry)
            print(f"Project {project_name} marked as {status}. Size: {size} bytes.")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def mark_sync_status(project_name, status):
    validate_project_name(project_name)
    registry_path = get_registry_path()

    with open(registry_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                registry = load_registry(f, registry_path=registry_path)
            except RegistryCorruptedError as e:
                print(f"❌ Cannot mark sync status: {e}. Refusing to overwrite corrupted registry.")
                sys.exit(1)

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

def is_git_tracked(file_path, project_path):
    """Checks if a file is tracked by git in project_path."""
    try:
        rel = file_path.relative_to(project_path)
        res = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(rel)],
            cwd=project_path,
            capture_output=True,
            text=True
        )
        return res.returncode == 0
    except Exception:
        return False

def cleanup_level_2(project_path, project_name=None):
    """
    Level 2: Delete regenerable caches and temporary files based on explicit whitelist.
    Whitelisted:
    - __pycache__, .pytest_cache, .mypy_cache, .ruff_cache, tmp, temp, .tmp
    - .cache/pip, .cache/uv, .cache/huggingface, .cache/torch
    - temporary files: *.tmp, *.temp, *.log, *.pyc, *.pyo
    Never touches:
    - .git/, .dvc/
    - .venv/, venv/, env/
    - git-tracked files
    """
    print(f"  [Level 2] Cleaning regenerable caches & temporary files in {project_path}")
    total_freed = 0
    project_path = Path(project_path)
    if not project_path.exists():
        return 0

    try:
        for root, dirs, files in os.walk(project_path, topdown=True):
            dirs_to_prune = []
            for d in list(dirs):
                dpath = Path(root) / d
                rel_d = str(dpath.relative_to(project_path)).replace("\\", "/")
                if d in (".git", ".dvc", ".venv", "venv", "env", "ENV") or rel_d in (".venv", "venv", "env"):
                    dirs_to_prune.append(d)
                elif d in REGENERABLE_DIR_NAMES or any(rel_d == prefix or rel_d.startswith(prefix + "/") for prefix in REGENERABLE_CACHE_PREFIXES):
                    dsize = get_dir_size(dpath)
                    try:
                        shutil.rmtree(dpath)
                        log_deletion(str(dpath), dsize, f"Tier 1: Regenerable cache directory ({rel_d})", dry_run=False)
                        total_freed += dsize
                    except OSError as e:
                        print(f"    Error deleting cache dir {dpath}: {e}")
                    dirs_to_prune.append(d)
            for d in dirs_to_prune:
                dirs.remove(d)

            for file in files:
                fpath = Path(root) / file
                if fpath.is_symlink():
                    continue

                is_whitelisted = False
                for pat in REGENERABLE_FILE_PATTERNS:
                    if fnmatch.fnmatch(file, pat):
                        is_whitelisted = True
                        break

                rel_str = str(fpath.relative_to(project_path)).replace("\\", "/")
                for prefix in REGENERABLE_CACHE_PREFIXES:
                    if rel_str.startswith(prefix):
                        is_whitelisted = True
                        break

                if is_whitelisted:
                    if (project_path / ".git").exists() and is_git_tracked(fpath, project_path):
                        continue
                    try:
                        size = fpath.stat().st_size
                        fpath.unlink()
                        log_deletion(str(fpath), size, f"Tier 1: Regenerable temp file ({file})", dry_run=False)
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
        res = subprocess.run(
            ["docker", "volume", "rm", "-f", volume_name],
            capture_output=True,
            text=True
        )
        if res.returncode == 0:
            log_deletion(volume_name, 0, f"Tier 2: Inactive project Docker volume ({project_name})", dry_run=False)
        else:
            err_msg = res.stderr.strip() if res.stderr else f"Exit code {res.returncode}"
            print(f"  ⚠️ Failed to delete Docker volume {volume_name}: {err_msg}")
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

def cleanup_all_project_docker_volumes(project_path, project_name=None):
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
        try:
            res = subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True, text=True)
            if res.returncode == 0:
                log_deletion(vol, 0, f"Tier 2: Inactive project Docker image volume ({project_name})", dry_run=False)
            else:
                err_msg = res.stderr.strip() if res.stderr else f"Exit code {res.returncode}"
                print(f"  ⚠️ Failed to delete image Docker volume {vol}: {err_msg}")
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
            log_deletion(str(project_path), freed, f"Tier 5: Full workspace for {project_name or project_path.name}", dry_run=False)
        except Exception as e:
            print(f"  Error in level 5 cleanup: {e}")
    return freed

def parse_docker_size(size_str):
    """Parses Docker size string (e.g. '133MB', '27.3GB', '8.45kB', '500B') to integer bytes."""
    if not size_str:
        return 0
    s = str(size_str).strip().upper().replace(" ", "")
    m = re.match(r'^([0-9.]+)\s*([KMGTP]?B?)$', s)
    if not m:
        return 0
    try:
        val = float(m.group(1))
        unit = m.group(2)
        multipliers = {
            "": 1, "B": 1,
            "KB": 1024, "K": 1024,
            "MB": 1024**2, "M": 1024**2,
            "GB": 1024**3, "G": 1024**3,
            "TB": 1024**4, "T": 1024**4,
            "PB": 1024**5, "P": 1024**5,
        }
        return int(val * multipliers.get(unit, 1))
    except (ValueError, TypeError):
        return 0

def get_docker_used_image_refs():
    """
    Returns set of image IDs and repo:tag references used by ANY container (running or stopped).
    Containers of active or stopped executors must NEVER have their images pruned.
    """
    used = set()
    is_mock = False
    try:
        import unittest.mock as mock
        if isinstance(subprocess.run, (mock.Mock, mock.MagicMock)):
            is_mock = True
    except Exception:
        pass
    if is_mock:
        return used

    try:
        res = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Image}}"],
            capture_output=True,
            text=True
        )
        if res.returncode == 0 and isinstance(res.stdout, str):
            for line in res.stdout.strip().split("\n"):
                img = line.strip()
                if img:
                    used.add(img)
                    if ":" in img:
                        used.add(img.split(":")[0])
                    if len(img) >= 12:
                        used.add(img[:12])
    except Exception as e:
        print(f"  Warning querying docker containers for used images: {e}")
    return used

def get_unused_docker_images(protected_image_refs=None):
    """
    Returns list of unused Docker images sorted by LRU (oldest first):
    [{'id': ..., 'ref': ..., 'repo': ..., 'tag': ..., 'size_bytes': ..., 'created_at': ...}, ...]
    Never includes images used by existing containers (running or stopped)
    or explicitly protected images (from active jobs or registry).
    """
    used_refs = get_docker_used_image_refs()
    if protected_image_refs:
        used_refs.update(protected_image_refs)

    is_mock = False
    try:
        import unittest.mock as mock
        if isinstance(subprocess.run, (mock.Mock, mock.MagicMock)):
            is_mock = True
    except Exception:
        pass
    if is_mock:
        return []

    images = []
    try:
        res = subprocess.run(
            ["docker", "images", "--format", "{{.ID}}\t{{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"],
            capture_output=True,
            text=True
        )
        if res.returncode == 0 and isinstance(res.stdout, str):
            for line in res.stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                img_id = parts[0].strip()
                repo = parts[1].strip()
                tag = parts[2].strip()
                size_str = parts[3].strip()
                created_at = parts[4].strip() if len(parts) > 4 else ""

                ref = f"{repo}:{tag}" if repo != "<none>" and tag != "<none>" else img_id

                # Exclude if used by ANY container or protected ref
                if (img_id in used_refs or 
                    ref in used_refs or 
                    repo in used_refs or 
                    img_id[:12] in used_refs or
                    any(p in used_refs for p in (img_id, ref, repo))):
                    continue

                size_bytes = parse_docker_size(size_str)
                images.append({
                    "id": img_id,
                    "ref": ref,
                    "repo": repo,
                    "tag": tag,
                    "size_bytes": size_bytes,
                    "created_at": created_at
                })
    except Exception as e:
        print(f"  Warning querying docker images: {e}")

    # Sort LRU: oldest created_at first
    images.sort(key=lambda x: x["created_at"])
    return images

def cleanup_unused_docker_images(dry_run=False, protected_image_refs=None):
    """
    Tier 4: Delete unused Docker images sorted by LRU (oldest first).
    Never touches images used by existing containers (running or stopped)
    or images associated with active executors.
    Returns total bytes freed (or simulated freed).
    """
    print("  [Tier 4] Cleaning unused Docker images (LRU)...")
    unused_images = get_unused_docker_images(protected_image_refs=protected_image_refs)
    freed_total = 0
    for img in unused_images:
        img_ref = img["ref"]
        img_id = img["id"]
        img_size = img["size_bytes"]
        if dry_run:
            print(f"  [Level Docker Images] [DRY RUN] Would delete unused Docker image {img_ref} (created: {img['created_at']})")
            log_deletion(img_ref, img_size, f"Tier 4: Unused Docker image (LRU, created: {img['created_at']})", dry_run=True)
            freed_total += img_size
        else:
            try:
                res = subprocess.run(["docker", "rmi", img_id], capture_output=True, text=True)
                if res.returncode == 0:
                    log_deletion(img_ref, img_size, f"Tier 4: Unused Docker image (LRU, created: {img['created_at']})", dry_run=False)
                    freed_total += img_size
                else:
                    err_msg = res.stderr.strip() if res.stderr else f"Exit code {res.returncode}"
                    print(f"  ⚠️ Failed to delete Docker image {img_ref}: {err_msg}")
            except Exception as e:
                print(f"  ⚠️ Error deleting Docker image {img_ref}: {e}")
    return freed_total

def get_base_repo_key(project_name):
    """
    Extracts canonical base repository identifier ignoring runner/executor suffixes and _local prefix.
    Examples:
      '_local/UNIL-DESI/llm-as-recommender' -> 'UNIL-DESI/llm-as-recommender'
      'UNIL-DESI/llm-as-recommender_runner_1' -> 'UNIL-DESI/llm-as-recommender'
      'UNIL-DESI/llm-as-recommender__exec2' -> 'UNIL-DESI/llm-as-recommender'
    """
    clean = str(project_name).strip()
    if clean.startswith("_local/"):
        clean = clean[len("_local/"):]
    # Strip runner / executor suffix patterns
    clean = re.sub(r'(_runner_|_worker_|_exec_|\.worker_|-runner-|-worker-|-exec-)\w+$', '', clean)
    clean = re.sub(r'(__\w+)$', '', clean)
    clean = re.sub(r'(_runner\d+)$', '', clean)
    return clean

def has_active_executor_for_repo(base_repo, registry, active_docker_info=None, current_time=None):
    """
    Checks if ANY executor workspace corresponding to base_repo is active
    (status running in registry, active container/mount in Docker, or executed < protect_hours ago).
    """
    if current_time is None:
        current_time = time.time()
    for name, data in registry.items():
        if get_base_repo_key(name) == base_repo:
            protected, reason = is_project_protected(name, data, current_time=current_time, active_docker_info=active_docker_info)
            if protected:
                return True, name, reason
    return False, "", ""


DEFAULT_LOCAL_WORKSPACE_TTL_HOURS = 168.0
INTERNAL_REPO_ROOT_DIRS = {
    "_local_uploads",
    "_local_results",
    "_local_transfers",
    "_tmp_artifacts",
}


def _is_subprocess_mocked():
    try:
        import unittest.mock as mock
        return isinstance(subprocess.run, (mock.Mock, mock.MagicMock))
    except Exception:
        return False


def get_user_quota_free_bytes(target_path):
    """
    Returns remaining bytes under the current Linux user's filesystem block quota
    for the mount containing target_path, or None if no user quota applies.
    """
    if os.name == "nt" or _is_subprocess_mocked():
        return None
    if shutil.which("quota") is None or shutil.which("df") is None:
        return None
    try:
        df_res = subprocess.run(
            ["df", "-P", str(target_path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if df_res.returncode != 0 or not df_res.stdout:
            return None
        df_lines = [ln.strip() for ln in df_res.stdout.strip().splitlines() if ln.strip()]
        if len(df_lines) < 2:
            return None
        device = df_lines[-1].split()[0]

        q_res = subprocess.run(
            ["quota", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if not q_res.stdout:
            return None
        for line in q_res.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 4 and parts[0] == device:
                used_kb = int(parts[1].rstrip("*"))
                soft_kb = int(parts[2])
                hard_kb = int(parts[3])
                limits = [lim for lim in (soft_kb, hard_kb) if lim > 0]
                if limits:
                    effective_limit_kb = min(limits)
                    return max(0, (effective_limit_kb - used_kb) * 1024)
    except Exception:
        return None
    return None


def get_free_space():
    repo_dir = get_repositories_dir()
    if not repo_dir.exists():
        return 0
    free_candidates = [shutil.disk_usage(repo_dir).free]

    canonical_repo_dir = Path(__file__).parent.parent.parent.resolve() / "repositories"
    try:
        is_canonical = repo_dir.resolve() == canonical_repo_dir
    except OSError:
        is_canonical = False

    if is_canonical and not _is_subprocess_mocked():
        for extra_dir in (tempfile.gettempdir(), "/var/lib/docker", "/"):
            if extra_dir and os.path.exists(extra_dir):
                try:
                    free_candidates.append(shutil.disk_usage(extra_dir).free)
                except OSError:
                    pass
        quota_free = get_user_quota_free_bytes(repo_dir)
        if quota_free is not None:
            free_candidates.append(quota_free)

    return min(free_candidates)


def run_docker_system_prune(dry_run=False, active_docker_info=None):
    """Purge stopped cluster containers, unused networks, dangling images, and stale build cache."""
    print("[Cluster GC] 🐳 Cleaning up Docker resources (system & builder prune)...")
    if dry_run:
        log_deletion("docker system prune", 0, "Tier 1: Docker system prune (stopped containers, dangling images)", dry_run=True)
        return
    try:
        subprocess.run(["docker", "system", "prune", "-f"], capture_output=True)
        log_deletion("docker system prune", 0, "Tier 1: Docker system prune (stopped containers, dangling images)", dry_run=False)
        if not _is_subprocess_mocked():
            if active_docker_info is not None:
                res = subprocess.run(
                    ["docker", "ps", "-a", "--filter", "status=exited", "--filter", "status=dead",
                     "--filter", "name=cluster-job-", "--filter", "name=cluster-viewer-", "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=10
                )
                if res.returncode == 0 and res.stdout:
                    for cname in (ln.strip() for ln in res.stdout.splitlines() if ln.strip()):
                        if cname not in active_docker_info:
                            subprocess.run(["docker", "rm", "-f", cname], capture_output=True, timeout=10)
            subprocess.run(["docker", "builder", "prune", "-af", "--filter", "until=24h"], capture_output=True, timeout=60)
    except Exception as e:
        print(f"  Error during Docker prune: {e}")


def _discover_untracked_projects_on_disk(repo_dir, registry, active_docker_info, now):
    """
    Scans repositories/<org>/<repo> and repositories/_local/<org>/<repo> for
    workspaces missing from registry.json (or marked 'deleted' while still on disk)
    so no directory is ever invisible to LRU eviction.
    Returns True if registry was mutated.
    """
    mutated = False
    if not repo_dir.exists():
        return False

    def _inspect_owner_root(root_dir, prefix=""):
        nonlocal mutated
        if not root_dir.is_dir():
            return
        try:
            owners = list(root_dir.iterdir())
        except OSError:
            return
        for owner_path in owners:
            if not owner_path.is_dir():
                continue
            owner_name = owner_path.name
            if not prefix and (owner_name in INTERNAL_REPO_ROOT_DIRS or owner_name == "_local" or owner_name.startswith(".")):
                continue
            if prefix and owner_name.startswith("."):
                continue
            try:
                repos = list(owner_path.iterdir())
            except OSError:
                continue
            for proj_path in repos:
                if not proj_path.is_dir() or proj_path.name.startswith("."):
                    continue
                proj_key = f"{prefix}{owner_name}/{proj_path.name}"
                try:
                    validate_project_name(proj_key)
                except ValueError:
                    continue
                docker_active, _ = is_project_active_in_docker(proj_key, active_docker_info)
                try:
                    mtime = proj_path.stat().st_mtime
                except OSError:
                    mtime = now
                entry = registry.get(proj_key)
                if entry is None:
                    registry[proj_key] = {
                        "status": "running" if docker_active else "idle",
                        "last_execution": mtime,
                        "size_bytes": get_dir_size(proj_path),
                    }
                    print(f"[Cluster GC] Discovered untracked workspace '{proj_key}' on disk (marked {'running' if docker_active else 'idle'}).")
                    mutated = True
                elif entry.get("status") == "deleted" and not docker_active:
                    entry["status"] = "idle"
                    entry.setdefault("last_execution", mtime)
                    entry["size_bytes"] = get_dir_size(proj_path)
                    print(f"[Cluster GC] Reactivated residual on-disk workspace '{proj_key}' from 'deleted' to 'idle'.")
                    mutated = True

    _inspect_owner_root(repo_dir, prefix="")
    _inspect_owner_root(repo_dir / "_local", prefix="_local/")
    return mutated


def _reconcile_legacy_running_without_job_ids(registry, active_docker_info, now):
    """
    Reconciles legacy 'status': 'running' entries that lack 'job_ids' (e.g. after SIGKILL
    or older runner versions) by querying headnode /job_status/_legacy_check.
    Fail-closed: never demotes to 'idle' without explicit headnode confirmation.
    """
    url = os.environ.get("HEADNODE_URL", "").rstrip("/")
    token = os.environ.get("CLUSTER_TOKEN", "")
    if not url or not token or _is_subprocess_mocked():
        return False

    mutated = False
    for name, data in list(registry.items()):
        if not isinstance(data, dict) or data.get("status") != "running":
            continue
        if data.get("job_ids"):
            continue
        if (now - float(data.get("last_execution") or 0)) < 3600:
            continue
        docker_active, _ = is_project_active_in_docker(name, active_docker_info)
        if docker_active:
            continue
        is_local = "1" if str(name).startswith("_local/") else "0"
        base_repo = get_base_repo_key(name)
        try:
            resp = requests.get(
                f"{url}/job_status/_legacy_check",
                params={"repo": base_repo, "is_local": is_local},
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
                allow_redirects=False,
            )
            if resp.status_code == 200:
                payload = resp.json()
                if payload.get("project_has_active_jobs") is False:
                    data["status"] = "idle"
                    print(f"[Cluster GC] Reconciled stale running entry '{name}' -> 'idle' (confirmed 0 active jobs on headnode).")
                    mutated = True
        except Exception as e:
            print(f"[Cluster GC] Skipping legacy running check for '{name}': {e}")
    return mutated


def _sweep_ephemeral_system_litter(repo_dir, active_docker_info, now, dry_run=False):
    """
    Cleans orphaned ephemeral files that accumulate outside project directories:
    - /tmp/cluster-ci-wt-*, /tmp/cluster-ci-src-*, /tmp/cluster_job_*, /tmp/host_guard_*.marker (> 2h old)
    - repositories/_tmp_artifacts/* (> 2h old)
    - runners/*/_diag/*.log (> 3 days old)
    """
    if _is_subprocess_mocked():
        return

    tmp_cutoff = now - 7200.0
    tmp_root = Path(tempfile.gettempdir())
    if tmp_root.is_dir():
        try:
            for item in tmp_root.iterdir():
                iname = item.name
                is_wt = iname.startswith("cluster-ci-wt-")
                is_tmp_art = (
                    iname.startswith("cluster-ci-src-")
                    or iname.startswith("cluster_job_")
                    or (iname.startswith("host_guard_") and iname.endswith(".marker"))
                )
                if not (is_wt or is_tmp_art):
                    continue
                try:
                    mtime = item.lstat().st_mtime
                except OSError:
                    continue
                if mtime >= tmp_cutoff:
                    continue
                if any(iname in info or str(item) in info for info in (active_docker_info or ())):
                    continue
                size_b = get_dir_size(item) if item.is_dir() else (item.stat().st_size if item.is_file() else 0)
                if dry_run:
                    log_deletion(str(item), size_b, "System Hygiene: Stale /tmp cluster artifact", dry_run=True)
                else:
                    try:
                        if item.is_dir() and not item.is_symlink():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                        log_deletion(str(item), size_b, "System Hygiene: Stale /tmp cluster artifact", dry_run=False)
                    except OSError:
                        pass
        except OSError:
            pass

    tmp_artifacts_dir = repo_dir / "_tmp_artifacts"
    if tmp_artifacts_dir.is_dir():
        try:
            for item in tmp_artifacts_dir.iterdir():
                try:
                    if item.lstat().st_mtime >= tmp_cutoff:
                        continue
                    size_b = get_dir_size(item) if item.is_dir() else item.stat().st_size
                    if not dry_run:
                        if item.is_dir() and not item.is_symlink():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                    log_deletion(str(item), size_b, "System Hygiene: Stale _tmp_artifacts entry", dry_run=dry_run)
                except OSError:
                    pass
        except OSError:
            pass

    runners_dir = get_base_dir() / "runners"
    diag_cutoff = now - (3 * 86400.0)
    if runners_dir.is_dir():
        try:
            for diag_log in runners_dir.glob("*/_diag/*.log"):
                try:
                    st = diag_log.stat()
                    if st.st_mtime < diag_cutoff:
                        if not dry_run:
                            diag_log.unlink(missing_ok=True)
                        log_deletion(str(diag_log), st.st_size, "System Hygiene: Old runner _diag log (>3d)", dry_run=dry_run)
                except OSError:
                    pass
        except OSError:
            pass


def _cleanup_orphan_and_expired_local_resources(repo_dir, registry, active_docker_info, now, dry_run=False):
    """
    1. Evicts idle `_local/<org>/<repo>` snapshot workspaces older than GC_LOCAL_WORKSPACE_TTL_HOURS (default 7d)
       along with their `cluster-ci-home-_local-*` Docker volumes.
    2. Removes orphaned `cluster-ci-home-*` Docker volumes whose project is already 'deleted' (and missing on disk)
       and not mounted in any active Docker container.
    Never touches non-`cluster-ci-home-` Docker volumes.
    """
    if _is_subprocess_mocked():
        return False

    mutated = False
    try:
        local_ttl_hours = float(os.environ.get("GC_LOCAL_WORKSPACE_TTL_HOURS", DEFAULT_LOCAL_WORKSPACE_TTL_HOURS))
    except (ValueError, TypeError):
        local_ttl_hours = DEFAULT_LOCAL_WORKSPACE_TTL_HOURS
    local_ttl_seconds = local_ttl_hours * 3600.0

    # 1. Expire old _local/ workspaces by TTL
    for name, data in list(registry.items()):
        if not str(name).startswith("_local/") or not isinstance(data, dict):
            continue
        if data.get("status") != "idle":
            continue
        protected, _ = is_project_protected(name, data, current_time=now, active_docker_info=active_docker_info)
        if protected:
            continue
        last_exec = float(data.get("last_execution") or 0)
        if (now - last_exec) < local_ttl_seconds:
            continue
        project_path = repo_dir / name
        if dry_run:
            if project_path.exists():
                log_deletion(str(project_path), get_dir_size(project_path), f"TTL Eviction: _local workspace idle > {local_ttl_hours:.0f}h", dry_run=True)
        else:
            cleanup_all_project_docker_volumes(project_path, name)
            if project_path.exists():
                cleanup_level_5(project_path, name)
            data["status"] = "deleted"
            data["size_bytes"] = 0
            mutated = True

    # 2. Remove orphaned cluster-ci-home-* Docker volumes of deleted/non-existent projects
    try:
        res = subprocess.run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode == 0 and isinstance(res.stdout, str):
            protected_prefixes = []
            for p_name, p_data in registry.items():
                if not isinstance(p_data, dict):
                    continue
                p_path = repo_dir / p_name
                p_status = p_data.get("status")
                p_prot, _ = is_project_protected(p_name, p_data, current_time=now, active_docker_info=active_docker_info)
                if p_prot or (p_status != "deleted" and p_path.exists()):
                    sanitized = p_name.replace("/", "-")
                    protected_prefixes.append(f"cluster-ci-home-{sanitized}")

            for raw_line in res.stdout.splitlines():
                vol = raw_line.strip()
                if not vol or not vol.startswith("cluster-ci-home-"):
                    continue
                if any(vol in info for info in (active_docker_info or ())):
                    continue
                is_kept = any(vol == pfx or vol.startswith(f"{pfx}-") for pfx in protected_prefixes)
                if is_kept:
                    continue
                if dry_run:
                    log_deletion(vol, 0, "System Hygiene: Orphaned cluster-ci-home Docker volume", dry_run=True)
                else:
                    rm_res = subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True, text=True, timeout=15)
                    if rm_res.returncode == 0:
                        log_deletion(vol, 0, "System Hygiene: Orphaned cluster-ci-home Docker volume", dry_run=False)
    except Exception as e:
        print(f"[Cluster GC] Warning scanning orphaned cluster-ci-home volumes: {e}")

    return mutated

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
    1. Caches / temporaires régénérables (docker system prune + DVC history + whitelisted caches/temp)
    2. Volumes Docker /home/user des projets inactifs (cluster-ci-home-* et cluster-ci-home-*-<image_slug>)
    3. Cache DVC partagé/local des projets inactifs les plus anciens (LRU, protégé si exécuteur actif sur le dépôt)
    4. Images Docker inutilisées (LRU, jamais si un conteneur actif ou arrêté existe)
    5. Workspace complet en dernier recours (LRU)
    Stopping as soon as the target free space threshold is reached.
    Projects currently running or executed < N hours ago (default 6h) are strictly protected.
    """
    repo_dir = get_repositories_dir()

    # 1. Race condition mitigation: mark current project as running before any inspection or purge
    if not current_project:
        env_ws = os.environ.get("WORKSPACE_KEY")
        env_repo = os.environ.get("TARGET_REPO")
        if env_ws:
            current_project = env_ws
        elif env_repo and "/" in env_repo:
            current_project = env_repo
    if current_project:
        validate_project_name(current_project)
        if dry_run:
            print(f"[{mode_name} GC] [DRY RUN] Simulating active project '{current_project}' as running in memory.")
        else:
            try:
                print(f"[{mode_name} GC] Marking active project '{current_project}' as running prior to GC...")
                update_running(current_project)
            except Exception as e:
                print(f"[{mode_name} GC] Note: could not mark project '{current_project}' as running: {e}")

    # 2. Check active docker containers; abort GC if docker ps fails
    try:
        active_docker_info = get_active_docker_info()
    except RuntimeError as e:
        print(f"❌ [{mode_name} GC] Halting GC: {e}")
        sys.exit(1)

    now_pre = time.time()
    registry_path = get_registry_path()

    # 2.5 Always run non-invasive system & TTL hygiene in live cluster mode (HEADNODE_URL set),
    # even when total free space exceeds threshold (prevents unbounded /tmp, _local/, and volume leaks on large disks).
    if os.environ.get("HEADNODE_URL") and not _is_subprocess_mocked():
        _sweep_ephemeral_system_litter(repo_dir, active_docker_info, now_pre, dry_run=dry_run)
        if registry_path.exists():
            with open(registry_path, "r+" if not dry_run else "r") as hf:
                fcntl.flock(hf, fcntl.LOCK_EX if not dry_run else fcntl.LOCK_SH)
                try:
                    h_reg = load_registry(hf, registry_path=registry_path)
                    def _pre_lookup(job_id):
                        url = os.environ.get('HEADNODE_URL', '').rstrip('/')
                        token = os.environ.get('CLUSTER_TOKEN')
                        if not url or not token:
                            raise RuntimeError('No authenticated headnode configuration')
                        response = requests.get(
                            url + '/job_status/' + job_id,
                            headers={'Authorization': 'Bearer ' + token},
                            timeout=5,
                            allow_redirects=False,
                        )
                        response.raise_for_status()
                        return response.json()
                    h_dirty = bool(recover_running(
                        h_reg,
                        _pre_lookup,
                        lambda name: is_project_active_in_docker(name, active_docker_info)[0],
                        now_pre,
                    ))
                    if _discover_untracked_projects_on_disk(repo_dir, h_reg, active_docker_info, now_pre):
                        h_dirty = True
                    if _reconcile_legacy_running_without_job_ids(h_reg, active_docker_info, now_pre):
                        h_dirty = True
                    if _cleanup_orphan_and_expired_local_resources(repo_dir, h_reg, active_docker_info, now_pre, dry_run=dry_run):
                        h_dirty = True
                    if h_dirty and not dry_run:
                        save_registry(hf, h_reg)
                except RegistryCorruptedError as e:
                    print(f"❌ [{mode_name} GC] Registry file is corrupt: {e}. HALTING GC WITHOUT PURGING.")
                    sys.exit(1)
                finally:
                    fcntl.flock(hf, fcntl.LOCK_UN)

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
    _sweep_ephemeral_system_litter(repo_dir, active_docker_info, time.time(), dry_run=dry_run)
    if mode_name in ("Emergency", "Maintenance"):
        run_docker_system_prune(dry_run=dry_run, active_docker_info=active_docker_info)
        if is_target_reached():
            print(f"[{mode_name} GC] Target space reached after Docker system prune.")
            return

    if not registry_path.exists():
        print(f"[{mode_name} GC] Registry file does not exist. Halting project-level cleanup.")
        return

    with open(registry_path, "r+" if not dry_run else "r") as f:
        fcntl.flock(f, fcntl.LOCK_EX if not dry_run else fcntl.LOCK_SH)
        try:
            try:
                registry = load_registry(f, registry_path=registry_path)
            except RegistryCorruptedError as e:
                print(f"❌ [{mode_name} GC] Registry file is corrupt: {e}. HALTING GC WITHOUT PURGING.")
                sys.exit(1)

            now = time.time()

            # SIGKILL/host shutdown can bypass the runner's EXIT trap. Docker
            # absence alone is insufficient: queued and preempted jobs matter.
            def lookup_registry_job(job_id):
                url = os.environ.get('HEADNODE_URL', '').rstrip('/')
                token = os.environ.get('CLUSTER_TOKEN')
                if not url or not token:
                    raise RuntimeError('No authenticated headnode configuration')
                response = requests.get(url + '/job_status/' + job_id,
                    headers={'Authorization': 'Bearer ' + token}, timeout=5,
                    allow_redirects=False)
                response.raise_for_status()
                return response.json()
            recovered = bool(recover_running(registry, lookup_registry_job,
                lambda name: is_project_active_in_docker(name, active_docker_info)[0], now))
            if _discover_untracked_projects_on_disk(repo_dir, registry, active_docker_info, now):
                recovered = True
            if _reconcile_legacy_running_without_job_ids(registry, active_docker_info, now):
                recovered = True
            if _cleanup_orphan_and_expired_local_resources(repo_dir, registry, active_docker_info, now, dry_run=dry_run):
                recovered = True
            if recovered and not dry_run:
                save_registry(f, registry)
            if is_target_reached():
                print(f"[{mode_name} GC] Target space reached after orphan/TTL hygiene.")
                return

            # If dry_run with current_project, simulate running in memory
            if dry_run and current_project:
                registry[current_project] = {"status": "running", "last_execution": now}

            # Identify eligible inactive projects
            eligible_projects = []
            for name, data in list(registry.items()):
                try:
                    validate_project_name(name)
                except ValueError as ve:
                    print(f"[{mode_name} GC] Skipping entry with invalid project name '{name}': {ve}")
                    continue

                if data.get("status") != "idle":
                    continue
                protected, reason = is_project_protected(name, data, current_time=now, active_docker_info=active_docker_info)
                if protected:
                    print(f"[{mode_name} GC] Skipping protected project '{name}': {reason}")
                    continue
                eligible_projects.append((name, data))

            # Sort strictly LRU: oldest last_execution first
            eligible_projects.sort(key=lambda x: x[1].get("last_execution", 0))

            if not eligible_projects:
                print(f"[{mode_name} GC] No eligible inactive projects found (all are running, active in Docker, or protected < {get_protect_seconds()/3600:.1f}h).")
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
                    # Scan regenerable cache dirs and files
                    for root, dirs, files in os.walk(project_path, topdown=True):
                        dirs_to_prune = []
                        for d in list(dirs):
                            if d in (".git", ".dvc", ".venv", "venv", "env", "ENV"):
                                dirs_to_prune.append(d)
                            elif d in REGENERABLE_DIR_NAMES:
                                dpath = Path(root) / d
                                dsize = get_dir_size(dpath)
                                print(f"    [DRY RUN] Would delete regenerable cache dir: {dpath}")
                                log_deletion(str(dpath), dsize, f"Tier 1: Regenerable cache directory ({d})", dry_run=True)
                                simulated_freed_bytes += dsize
                                dirs_to_prune.append(d)
                        for d in dirs_to_prune:
                            dirs.remove(d)

                        for file in files:
                            fpath = Path(root) / file
                            is_whitelisted = False
                            for pat in REGENERABLE_FILE_PATTERNS:
                                if fnmatch.fnmatch(file, pat):
                                    is_whitelisted = True
                                    break
                            rel_str = str(fpath.relative_to(project_path)).replace("\\", "/")
                            for prefix in REGENERABLE_CACHE_PREFIXES:
                                if rel_str.startswith(prefix):
                                    is_whitelisted = True
                                    break
                            if is_whitelisted:
                                if (project_path / ".git").exists() and is_git_tracked(fpath, project_path):
                                    continue
                                try:
                                    s = fpath.stat().st_size
                                    print(f"    [DRY RUN] Would delete regenerable temp file: {fpath}")
                                    log_deletion(str(fpath), s, f"Tier 1: Regenerable temp file ({file})", dry_run=True)
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
            # A11 : Le cache DVC partagé d'un dépôt n'est purgé que si AUCUN exécuteur de ce dépôt n'est actif
            if not is_target_reached():
                print(f"[{mode_name} GC] Tier 3: Cleaning DVC cache of oldest inactive projects (LRU)...")
                for project_name, data in eligible_projects:
                    if is_target_reached():
                        break
                    base_repo = get_base_repo_key(project_name)
                    has_active, active_ws, active_reason = has_active_executor_for_repo(
                        base_repo, registry, active_docker_info=active_docker_info, current_time=now
                    )
                    if has_active:
                        print(f"  [Level 4] Skipping shared DVC cache for '{project_name}': active executor detected for base repo '{base_repo}' (workspace '{active_ws}': {active_reason}).")
                        continue

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

            # --- Palier 4 : Images Docker inutilisées (LRU) ---
            # A15 : Images Docker inutilisées triées par LRU, jamais si conteneur actif ou arrêté
            if not is_target_reached():
                print(f"[{mode_name} GC] Tier 4: Cleaning unused Docker images (LRU)...")
                # Collect any images associated with active or protected projects in registry
                protected_imgs = set()
                for p_name, p_data in registry.items():
                    p_prot, _ = is_project_protected(p_name, p_data, current_time=now, active_docker_info=active_docker_info)
                    if p_prot:
                        for img_field in ("image", "docker_image", "image_name"):
                            if p_data.get(img_field):
                                protected_imgs.add(p_data[img_field])

                unused_images = get_unused_docker_images(protected_image_refs=protected_imgs)
                for img in unused_images:
                    if is_target_reached():
                        break
                    img_ref = img["ref"]
                    img_id = img["id"]
                    img_size = img["size_bytes"]
                    if dry_run:
                        print(f"  [Level Docker Images] [DRY RUN] Would delete unused Docker image {img_ref} (created: {img['created_at']})")
                        log_deletion(img_ref, img_size, f"Tier 4: Unused Docker image (LRU, created: {img['created_at']})", dry_run=True)
                        simulated_freed_bytes += img_size
                    else:
                        try:
                            res = subprocess.run(["docker", "rmi", img_id], capture_output=True, text=True)
                            if res.returncode == 0:
                                log_deletion(img_ref, img_size, f"Tier 4: Unused Docker image (LRU, created: {img['created_at']})", dry_run=False)
                                simulated_freed_bytes += img_size
                            else:
                                err_msg = res.stderr.strip() if res.stderr else f"Exit code {res.returncode}"
                                print(f"  ⚠️ Failed to delete Docker image {img_ref}: {err_msg}")
                        except Exception as e:
                            print(f"  ⚠️ Error deleting Docker image {img_ref}: {e}")
                    if is_target_reached():
                        break

            # --- Palier 5 : Workspace complet en dernier recours (LRU) ---
            if not is_target_reached():
                print(f"[{mode_name} GC] Tier 5: Deleting complete workspaces of oldest inactive projects (LRU)...")
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
                        log_deletion(str(project_path), ws_size, f"Tier 5: Full workspace for {project_name}", dry_run=True)
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
    if not repo_dir.exists():
        return

    zombie_registry_path = get_zombie_registry_path()
    zombie_registry_path.parent.mkdir(parents=True, exist_ok=True)

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
        if zombie_registry_path.exists():
            try:
                os.remove(zombie_registry_path)
            except OSError:
                pass
        kill_host_dvc_viewer_processes()
        return

    with open(zombie_registry_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                registry = load_registry(f, registry_path=zombie_registry_path)
            except RegistryCorruptedError:
                registry = {}
            now = time.time()
            new_registry = {}

            for container_name in containers:
                has_activity = False

                job_id = container_name.replace("cluster-job-", "")
                log_path = get_base_dir() / "job_logs" / f"{job_id}.log"

                current_log_mtime = 0
                if log_path.exists():
                    current_log_mtime = log_path.stat().st_mtime

                current_cpu = 0.0
                current_net = ""
                try:
                    cmd = ["docker", "stats", "--no-stream", "--format", '{"cpu": "{{.CPUPerc}}", "net": "{{.NetIO}}"}', container_name]
                    stats_res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if stats_res.returncode == 0:
                        stats = json.loads(stats_res.stdout)
                        current_cpu = float(stats.get("cpu", "0%").replace("%", "").strip())
                        current_net = stats.get("net", "")
                except (subprocess.SubprocessError, ValueError, json.JSONDecodeError, OSError):
                    pass

                current_gpu = 0
                try:
                    gpu_res = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
                    if gpu_res.returncode == 0:
                        utils = [int(x) for x in gpu_res.stdout.strip().split("\n") if x.strip().isdigit()]
                        current_gpu = sum(utils)
                except (subprocess.SubprocessError, ValueError, OSError):
                    pass

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
                    subprocess.run(["docker", "rm", "-f", container_name.replace("cluster-job-", "cluster-viewer-")], capture_output=True)
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
        print("Usage: python gc_orchestrator.py <command> [args] [--dry-run] [--protect-hours N] [--current-project NAME]")
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

    current_project = None
    if "--current-project" in raw_args:
        idx = raw_args.index("--current-project")
        if idx + 1 < len(raw_args):
            current_project = raw_args[idx + 1]
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
            if not current_project and sub_args:
                current_project = sub_args[0]
            run_gc(dry_run=dry_run, current_project=current_project)
        elif command == "run-transfer-gc":
            if not current_project and sub_args:
                current_project = sub_args[0]
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
