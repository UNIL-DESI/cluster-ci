import time
import hmac
import requests
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

import socket
import psutil
import subprocess
import logging
import uuid
import threading
import json
import tempfile
import shutil
import signal
import datetime
from flask import abort, Flask, jsonify, send_from_directory, send_file, request, Response

try:
    from src.config.defaults import DEFAULT_RESOURCES
    DEFAULT_RAM_GB = float(DEFAULT_RESOURCES.get("ram_gb", 10.0))
except ImportError:
    DEFAULT_RAM_GB = 10.0

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADNODE_URL = os.environ.get("HEADNODE_URL")
if not HEADNODE_URL:
    if "pytest" in sys.modules or "unittest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") or (sys.argv and "test" in sys.argv[0]):
        HEADNODE_URL = "http://localhost:5000"
    else:
        logger.critical("❌ Error: HEADNODE_URL environment variable is missing.")
        sys.exit(1)
CLUSTER_TOKEN = os.environ.get("CLUSTER_TOKEN")
BASE_DIR = os.environ.get("CLUSTER_CI_BASE_DIR", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
LOGS_DIR = os.path.join(BASE_DIR, "job_logs")
REPOS_DIR = os.path.join(BASE_DIR, "repositories")
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(REPOS_DIR, exist_ok=True)

def get_headers():
    headers = {}
    if CLUSTER_TOKEN:
        headers["Authorization"] = f"Bearer {CLUSTER_TOKEN}"
    return headers

def kill_container_processes_on_host(container_name):
    """Inspects the given container's host PID, and forcefully SIGKILLs all processes within its namespace directly on the host."""
    try:
        res = subprocess.run(["docker", "inspect", "--format", "{{.State.Pid}}", container_name],
                             capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            pid = int(res.stdout.strip())
            if pid > 0:
                logger.info(f"🎯 Host-level eradication: Killing all processes in container {container_name} (Host PID: {pid})")
                try:
                    parent = psutil.Process(pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    parent.kill()
                    logger.info(f"✅ Successfully killed host process tree of container {container_name}")
                except psutil.NoSuchProcess:
                    pass
    except Exception as e:
        logger.error(f"Error executing host-level container process eradication for {container_name}: {e}")

def safe_docker_rm_f(container_names, timeout=8):
    """Safely and robustly removes docker containers by first killing their host process tree,
    then running docker rm -f under a try-except block with a timeout to prevent Docker daemon lockups.
    """
    if isinstance(container_names, str):
        container_names = [container_names]
    
    for container in container_names:
        logger.info(f"🛡️ Safe Docker Purge: Removing container {container}...")
        # First try host-level PID eradication to avoid Docker lockups
        kill_container_processes_on_host(container)
        try:
            res = subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=timeout)
            if res.returncode == 0:
                logger.info(f"✅ Successfully removed container {container}")
            else:
                logger.warning(f"Warning: docker rm -f {container} returned exit code {res.returncode}. Stderr: {res.stderr.decode(errors='replace') if isinstance(res.stderr, bytes) else res.stderr}")
        except subprocess.TimeoutExpired:
            logger.error(f"❌ TimeoutExpired: docker rm -f {container} timed out after {timeout} seconds")
        except Exception as e:
            logger.error(f"❌ Error removing container {container}: {e}")

def prune_all_git_worktrees():
    """Scans REPOS_DIR and runs `git worktree prune --expire now` on all git repositories."""
    if not os.path.exists(REPOS_DIR):
        return

    logger.info("🌿 [PRUNE WORKTREES] Pruning stale git worktrees across all repositories in REPOS_DIR...")
    try:
        for root, dirs, _ in os.walk(REPOS_DIR):
            if ".git" in dirs:
                # Don't recurse deeper into this git directory
                dirs.remove(".git")
                try:
                    logger.debug(f"Pruning git worktrees for {root}...")
                    subprocess.run(
                        ["git", "worktree", "prune", "--expire", "now"],
                        cwd=root,
                        capture_output=True,
                        timeout=10
                    )
                except Exception as e:
                    logger.warning(f"Error pruning git worktree in {root}: {e}")
    except Exception as e:
        logger.error(f"Error while scanning repositories for worktree pruning: {e}")

def purge_orphan_runners_and_containers(job_id=None):
    """Performs JIT (Just-In-Time) purge of orphan docker containers and runner processes.
    
    If job_id is provided, avoids destroying containers associated with this job.
    Otherwise, destroys all cluster-job-* and cluster-viewer-* containers.
    """
    logger.info(f"🧹 Performing JIT (Just-In-Time) purge of orphan runners and containers (Job ID context: {job_id})")
    
    # Unleash proactive Ollama Host VRAM Purge to instantly reclaim physical resources before starting/cleaning up
    purge_ollama_vram_on_host()
    
    # Prune stale git worktrees across all local repositories
    prune_all_git_worktrees()
    
    # 1. Docker JIT Container Purge
    safe_job_id = job_id.replace('/', '-') if job_id else None
    expected_containers = {f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"} if safe_job_id else set()
    
    try:
        res = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=cluster-job-", "--filter", "name=cluster-viewer-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15
        )
        if res.returncode == 0:
            containers = [c.strip() for c in res.stdout.split("\n") if c.strip()]
            for container in containers:
                if container not in expected_containers:
                    logger.warning(f"🔥 JIT Purge: Destroying orphan/zombie container {container}...")
                    safe_docker_rm_f(container, timeout=8)
        else:
            logger.error(f"JIT Purge: Failed to list docker containers: {res.stderr}")
    except Exception as e:
        logger.error(f"JIT Purge: Error during docker container purge: {e}")
        
    # 2. Host Orphan Process Purge (including dvc-viewer, python runners and orphan custom Ollama servers)
    my_pid = os.getpid()
    logger.info("Scanning for orphan runner/viewer processes on host...")
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            pid = proc.info.get('pid')
            if pid == my_pid:
                continue
            
            cmdline = proc.info.get('cmdline') or []
            cmdline_str = " ".join(cmdline).lower()
            name = (proc.info.get('name') or "").lower()
            
            is_orphan_runner = False
            if "cluster-ci-run" in cmdline_str:
                # Avoid killing GHA runner delegation process.
                # The delegation process (running on the headnode to submit and wait) does NOT have CLUSTER_CI_MODE=executor.
                # Only the executor processes running the actual job docker container have CLUSTER_CI_MODE=executor.
                try:
                    environ = proc.environ()
                    if environ.get("CLUSTER_CI_MODE") == "executor":
                        is_orphan_runner = True
                    else:
                        logger.info(f"Skipping non-executor cluster-ci process (PID: {pid}, cmd: {cmdline})")
                except Exception as e:
                    # In case of environment read failure, check arguments length to be safe.
                    # GHA delegation runner: /usr/local/bin/cluster-ci-run <repo> <branch> <token> (len >= 4)
                    # Worker executor: /usr/local/bin/cluster-ci-run <repo> <branch> (len == 3)
                    if len(cmdline) == 3:
                        is_orphan_runner = True
                    else:
                        logger.warning(f"Could not read environment for process {pid} ({e}). Skipping to avoid killing GHA runner.")
            elif "gc_orchestrator" in cmdline_str:
                is_orphan_runner = True
            if "dvc-viewer" in cmdline_str or name == "dvc-viewer":
                is_orphan_runner = True
            
            # Target custom local Ollama processes running on 11435 to avoid port collision and memory leak
            if "ollama" in name or "llama" in name:
                is_custom_ollama = False
                for arg in cmdline:
                    if "11435" in arg:
                        is_custom_ollama = True
                
                # If we can access environment variables of the process, double-check OLLAMA_HOST
                try:
                    environ = proc.environ()
                    if "11435" in environ.get("OLLAMA_HOST", ""):
                        is_custom_ollama = True
                except Exception:
                    pass
                
                if is_custom_ollama:
                    logger.warning(f"🔥 JIT Purge: Detected orphaned custom Ollama process (PID: {pid}, cmd: {cmdline})")
                    is_orphan_runner = True
            
            if is_orphan_runner:
                logger.warning(f"🔥 JIT Purge: Killing host orphan process (PID: {pid}, name: {name}, cmd: {cmdline})")
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

def purge_ollama_vram_on_host():
    """Contact local Ollama services on host (both standard port 11434 and custom port 11435)
    to unload all active models and free GPU VRAM instantly.
    """
    ports = [11434, 11435]
    for port in ports:
        logger.info(f"📡 Requesting local Ollama service on port {port} to purge all models from GPU VRAM...")
        ollama_url = f"http://127.0.0.1:{port}"
        try:
            # 1. Get list of active/loaded models in memory
            resp = requests.get(f"{ollama_url}/api/ps", timeout=3)
            if resp.status_code == 200:
                models_data = resp.json()
                models = models_data.get("models", [])
                if not models:
                    logger.info(f"Ollama memory on port {port} is already clean (0 models loaded).")
                    continue
                    
                for m in models:
                    name = m.get("name") or m.get("model")
                    if name:
                        logger.warning(f"🔥 Forcing unload of Ollama model '{name}' on port {port} from GPU VRAM...")
                        # Sending keep_alive: 0 or keep_alive: "0s" forces immediate unload
                        requests.post(
                            f"{ollama_url}/api/generate",
                            json={"model": name, "keep_alive": 0},
                            timeout=3
                        )
                logger.info(f"✅ Successfully requested Ollama on port {port} to unload all models.")
            else:
                logger.info(f"Ollama API /api/ps on port {port} returned status {resp.status_code}. Skipping VRAM purge on this port.")
        except Exception as e:
            logger.info(f"Ollama service on port {port} is not running or unreachable: {e}. Skipping VRAM purge on this port.")

def kill_dvc_viewer_processes():
    # Deprecated wrapper: delegate to our robust purge function
    purge_orphan_runners_and_containers()

# Generate or load a persistent worker ID
WORKER_ID_FILE = "worker_id.txt"
if os.path.exists(WORKER_ID_FILE):
    with open(WORKER_ID_FILE, 'r') as f:
        WORKER_ID = f.read().strip()
else:
    WORKER_ID = str(uuid.uuid4())
    with open(WORKER_ID_FILE, 'w') as f:
        f.write(WORKER_ID)

HOSTNAME = socket.gethostname()
AGENT_PORT = int(os.environ.get("AGENT_PORT", 6000))
SERVICE_URL = os.environ.get("SERVICE_URL", f"http://{HOSTNAME}:{AGENT_PORT}")

# Role detection (A14: no hardcoded hostnames or IPs; role comes from environment or config file)
ROLE = os.environ.get("CLUSTER_CI_ROLE")
if not ROLE and os.path.exists("/etc/cluster-ci/role"):
    try:
        with open("/etc/cluster-ci/role", "r") as f:
            ROLE = f.read().strip()
    except Exception:
        pass
if not ROLE:
    ROLE = "worker"

# Global state for multi-runner tracking (A11)
active_executors = {}  # runner_id -> dict(job_id=..., process=..., is_parallel=..., start_time=..., repo=..., branch=...)
current_job_id = None
current_process = None
job_lock = threading.Lock()
startup_heartbeat_event = threading.Event()


def get_ram_info():
    mem = psutil.virtual_memory()
    total_gb = mem.total / (1024**3)
    available_gb = mem.available / (1024**3)
    return total_gb, available_gb

def get_storage_info():
    target_path = REPOS_DIR if os.path.exists(REPOS_DIR) else BASE_DIR
    try:
        usage = shutil.disk_usage(target_path)
        total_gb = usage.total / (1024**3)
        available_gb = usage.free / (1024**3)
        return total_gb, available_gb
    except Exception as e:
        logger.error(f"Error getting storage info for '{target_path}': {e}")
        return None, None

def get_cpu_info():
    """Detects CPU count generic for any Linux or host environment,
    taking into account cgroups limits (v1 and v2), affinity masks (sched_getaffinity),
    and environment variable overrides.
    """
    # 1. Environment variable override
    env_cpus = os.environ.get("CLUSTER_CI_CPUS") or os.environ.get("CLUSTER_CI_WORKER_CPUS")
    if env_cpus:
        try:
            val = int(env_cpus)
            if val > 0:
                return val
        except ValueError:
            pass

    cgroup_cpus = None
    # 2. Check cgroups v2: /sys/fs/cgroup/cpu.max contains "quota period"
    try:
        if os.path.exists("/sys/fs/cgroup/cpu.max"):
            with open("/sys/fs/cgroup/cpu.max", "r") as f:
                parts = f.read().strip().split()
                if len(parts) >= 2 and parts[0] != "max":
                    quota = float(parts[0])
                    period = float(parts[1])
                    if period > 0:
                        cgroup_cpus = quota / period
    except Exception as e:
        logger.debug(f"cgroups v2 cpu detection failed: {e}")

    # 3. Check cgroups v1: cpu.cfs_quota_us and cpu.cfs_period_us
    if cgroup_cpus is None:
        try:
            q_file = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
            p_file = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
            if os.path.exists(q_file) and os.path.exists(p_file):
                with open(q_file, "r") as qf, open(p_file, "r") as pf:
                    quota = float(qf.read().strip())
                    period = float(pf.read().strip())
                    if quota > 0 and period > 0:
                        cgroup_cpus = quota / period
        except Exception as e:
            logger.debug(f"cgroups v1 cpu detection failed: {e}")

    # 4. Check sched_getaffinity on Linux (takes into account taskset/cpuset)
    affinity_cpus = None
    if hasattr(os, "sched_getaffinity"):
        try:
            affinity_cpus = len(os.sched_getaffinity(0))
        except Exception as e:
            logger.warning(f"Error reading CPU affinity mask via sched_getaffinity: {e}")
            affinity_cpus = None

    # 5. Fallback os.cpu_count()
    sys_cpus = os.cpu_count()

    candidates = []
    if sys_cpus is not None and sys_cpus > 0:
        candidates.append(sys_cpus)
    if affinity_cpus is not None and affinity_cpus > 0:
        candidates.append(affinity_cpus)
    if cgroup_cpus is not None and cgroup_cpus > 0:
        import math
        candidates.append(max(1, int(math.ceil(cgroup_cpus))))

    if not candidates:
        logger.error("Unable to determine CPU count from environment, cgroups, affinity, or system.")
        return None

    return max(1, min(candidates))

def get_arch_info():
    """Detects system architecture (e.g. 'x86_64', 'aarch64'),
    with optional override via CLUSTER_CI_ARCH.
    """
    env_arch = os.environ.get("CLUSTER_CI_ARCH")
    if env_arch:
        return env_arch.strip()
    import platform
    arch = platform.machine() or "x86_64"
    if arch.lower() in ("amd64", "x86-64"):
        return "x86_64"
    elif arch.lower() in ("arm64", "aarch64"):
        return "aarch64"
    return arch

def parse_nvidia_smi_output(stdout_text, total_ram_gb=None, available_ram_gb=None, force_unified=None):
    """Parses nvidia-smi CSV output and returns structured GPU capacity data.
    
    Robust against:
    - Grace-Blackwell GB10 / unified memory (nvidia-smi outputs [N/A] or [Not Supported])
    - Multi-GPU discrete setups (e.g. 2x RTX 3090)
    - Systems with 0 GPUs (empty stdout or errors)
    - Manual overrides via force_unified or CLUSTER_CI_UNIFIED_MEMORY
    """
    if force_unified is None:
        env_unified = os.environ.get("CLUSTER_CI_UNIFIED_MEMORY")
        if env_unified is not None:
            force_unified = env_unified.lower() in ("1", "true", "yes", "on")

    if not stdout_text or not stdout_text.strip():
        return {
            "gpu_name": "N/A",
            "total_vram_gb": 0.0,
            "gpu_count": 0,
            "available_vram_gb": 0.0,
            "vram_per_gpu": [],
            "unified_memory": 0,
            "gpu_details": []
        }

    lines = [l.strip() for l in stdout_text.strip().split("\n") if l.strip()]
    if not lines:
        return {
            "gpu_name": "N/A",
            "total_vram_gb": 0.0,
            "gpu_count": 0,
            "available_vram_gb": 0.0,
            "vram_per_gpu": [],
            "unified_memory": 0,
            "gpu_details": []
        }

    raw_gpus = []
    has_na_memory = False
    has_gb10_or_grace = False

    for idx, line in enumerate(lines):
        parts = [p.strip() for p in line.split(",")]
        # Determine column layout:
        # 5 cols: index, gpu_name, memory.total, memory.used, memory.free
        # 4 cols: gpu_name, memory.total, memory.used, memory.free
        # 3 cols: gpu_name, memory.total, memory.free
        # 2 cols: gpu_name, memory.total
        if len(parts) >= 5 and parts[0].isdigit():
            gpu_idx = int(parts[0])
            name = parts[1]
            tot_str = parts[2]
            used_str = parts[3]
            free_str = parts[4]
        elif len(parts) == 4:
            gpu_idx = idx
            name = parts[0]
            tot_str = parts[1]
            used_str = parts[2]
            free_str = parts[3]
        elif len(parts) == 3:
            gpu_idx = idx
            name = parts[0]
            tot_str = parts[1]
            used_str = "N/A"
            free_str = parts[2]
        else:
            gpu_idx = idx
            name = parts[0] if parts else "Unknown GPU"
            tot_str = parts[1] if len(parts) > 1 else "N/A"
            used_str = "N/A"
            free_str = "N/A"

        def _is_na(s):
            clean = s.upper().replace("[", "").replace("]", "").strip()
            return clean in ("N/A", "NOT SUPPORTED", "NONE", "")

        is_tot_na = _is_na(tot_str)
        is_free_na = _is_na(free_str)
        if is_tot_na or is_free_na:
            has_na_memory = True

        name_upper = name.upper()
        if any(kw in name_upper for kw in ("GB10", "GRACE", "GH200", "GB200")):
            has_gb10_or_grace = True

        raw_gpus.append({
            "index": gpu_idx,
            "name": name,
            "tot_str": tot_str,
            "used_str": used_str,
            "free_str": free_str,
            "is_na": is_tot_na or is_free_na
        })

    gpu_count = len(raw_gpus)
    first_name = raw_gpus[0]["name"] if raw_gpus else "Unknown GPU"
    display_name = f"{gpu_count}x {first_name}" if gpu_count > 1 else first_name

    # Determine unified memory
    if force_unified is not None:
        is_unified = bool(force_unified)
    else:
        is_unified = has_gb10_or_grace or has_na_memory

    if is_unified:
        if total_ram_gb is None or available_ram_gb is None:
            r_tot, r_avail = get_ram_info()
        else:
            r_tot, r_avail = total_ram_gb, available_ram_gb
        r_tot = round(r_tot, 2)
        r_avail = round(r_avail, 2)
        r_used = round(max(0.0, r_tot - r_avail), 2)

        vram_per_gpu = [r_tot for _ in range(gpu_count)]
        gpu_details = [
            {
                "index": g["index"],
                "name": g["name"],
                "total_vram_gb": r_tot,
                "used_vram_gb": r_used,
                "free_vram_gb": r_avail,
                "unified_memory": 1
            }
            for g in raw_gpus
        ]
        return {
            "gpu_name": display_name,
            "total_vram_gb": r_tot,
            "gpu_count": gpu_count,
            "available_vram_gb": r_avail,
            "vram_per_gpu": vram_per_gpu,
            "unified_memory": 1,
            "gpu_details": gpu_details
        }

    # Discrete GPUs
    vram_per_gpu = []
    gpu_details = []
    free_values = []
    for g in raw_gpus:
        try:
            tot_mb = float(g["tot_str"])
            tot_gb = round(tot_mb / 1024.0, 2)
        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to parse discrete GPU total VRAM '{g.get('tot_str')}' for GPU {g.get('index')}: {e}")
            tot_gb = None

        try:
            free_mb = float(g["free_str"])
            free_gb = round(free_mb / 1024.0, 2)
        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to parse discrete GPU free VRAM '{g.get('free_str')}' for GPU {g.get('index')}: {e}")
            free_gb = None

        try:
            used_mb = float(g["used_str"])
            used_gb = round(used_mb / 1024.0, 2)
        except (ValueError, TypeError):
            if tot_gb is not None and free_gb is not None:
                used_gb = round(max(0.0, tot_gb - free_gb), 2)
            else:
                used_gb = None

        vram_per_gpu.append(tot_gb)
        free_values.append(free_gb)
        gpu_details.append({
            "index": g["index"],
            "name": g["name"],
            "total_vram_gb": tot_gb,
            "used_vram_gb": used_gb,
            "free_vram_gb": free_gb,
            "unified_memory": 0
        })

    valid_tot = [v for v in vram_per_gpu if v is not None]
    total_vram_gb = max(valid_tot) if valid_tot else None
    valid_free = [f for f in free_values if f is not None]
    available_vram_gb = min(valid_free) if valid_free else None

    return {
        "gpu_name": display_name,
        "total_vram_gb": total_vram_gb,
        "gpu_count": gpu_count,
        "available_vram_gb": available_vram_gb,
        "vram_per_gpu": vram_per_gpu,
        "unified_memory": 0,
        "gpu_details": gpu_details
    }

def get_gpu_info():
    """Detects GPU name, per-GPU VRAM, GPU count, available VRAM, and unified memory via nvidia-smi.
    Returns (gpu_name, total_vram_gb, gpu_count, available_vram_gb, vram_per_gpu, unified_memory).
    """
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,gpu_name,memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10
        )
        if res.returncode != 0 or not res.stdout.strip():
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=gpu_name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10
            )
        if res.returncode == 0 and res.stdout.strip():
            data = parse_nvidia_smi_output(res.stdout)
            return (
                data["gpu_name"],
                data["total_vram_gb"],
                data["gpu_count"],
                data["available_vram_gb"],
                data["vram_per_gpu"],
                data["unified_memory"]
            )
    except Exception as e:
        logger.warning(f"GPU detection failed: {e}")

    data = parse_nvidia_smi_output("")
    return (
        data["gpu_name"],
        data["total_vram_gb"],
        data["gpu_count"],
        data["available_vram_gb"],
        data["vram_per_gpu"],
        data["unified_memory"]
    )

def parse_docker_size(size_str):
    """Converts Docker human-readable size strings (e.g., '191MB', '1.46GB', '420kB', '500B')
    into integer bytes. Returns None if size_str cannot be parsed.
    """
    if not size_str or not isinstance(size_str, str):
        return None
    s = size_str.strip().upper()
    units = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000 * 1000,
        "MIB": 1024 * 1024,
        "GB": 1000 * 1000 * 1000,
        "GIB": 1024 * 1024 * 1024,
        "TB": 1000 * 1000 * 1000 * 1000,
        "TIB": 1024 * 1024 * 1024 * 1024,
    }
    m = re.match(r"^([0-9.]+)\s*([A-Z]*)$", s)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except (ValueError, TypeError):
        return None
    unit = m.group(2) or "B"
    if unit not in units:
        return None
    mult = units[unit]
    return int(val * mult)

def get_docker_images():
    """Declares local Docker images as {"repo:tag": size_bytes} via `docker image ls`.
    On error/failure, logs the error and returns None (never silent {}).
    """
    try:
        res = subprocess.run(
            ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}"],
            capture_output=True, text=True, timeout=10
        )
        if res.returncode != 0:
            err_msg = res.stderr.strip() if res.stderr else "non-zero exit code"
            logger.error(f"Error querying docker images via 'docker image ls' (exit code {res.returncode}): {err_msg}")
            return None

        images = {}
        for line in res.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                tag_name = parts[0].strip()
                size_str = parts[1].strip()
            else:
                tokens = line.split()
                if len(tokens) >= 2:
                    tag_name = tokens[0].strip()
                    size_str = tokens[1].strip()
                else:
                    continue

            if tag_name.startswith("<none>"):
                continue

            images[tag_name] = parse_docker_size(size_str)
        return images
    except Exception as e:
        logger.error(f"Error querying docker images via 'docker image ls': {e}")
        return None

def get_worker_capabilities():
    """Aggregates all worker capacities into a single structured dictionary."""
    total_ram_gb, available_ram_gb = get_ram_info()
    total_storage_gb, available_storage_gb = get_storage_info()
    gpu_name, total_vram_gb, gpu_count, available_vram_gb, vram_per_gpu, unified_memory = get_gpu_info()
    cpus = get_cpu_info()
    arch = get_arch_info()

    active_runners_list = []
    with job_lock:
        for r_id, ex in active_executors.items():
            active_runners_list.append({
                "runner_id": r_id,
                "job_id": ex.get("job_id"),
                "repo": ex.get("repo"),
                "is_parallel": ex.get("is_parallel", False),
                "start_time": ex.get("start_time")
            })

    caps = {
        "cpus": cpus,
        "ram_gb": round(total_ram_gb, 2) if total_ram_gb is not None else None,
        "total_ram_gb": round(total_ram_gb, 2) if total_ram_gb is not None else None,
        "available_ram_gb": round(available_ram_gb, 2) if available_ram_gb is not None else None,
        "total_storage_gb": round(total_storage_gb, 2) if total_storage_gb is not None else None,
        "available_storage_gb": round(available_storage_gb, 2) if available_storage_gb is not None else None,
        "disk_free_gb": round(available_storage_gb, 2) if available_storage_gb is not None else None,
        "gpu_name": gpu_name,
        "gpu_count": gpu_count,
        "total_vram_gb": total_vram_gb,
        "available_vram_gb": available_vram_gb,
        "vram_per_gpu": vram_per_gpu,
        "unified_memory": unified_memory,
        "arch": arch,
        "docker_images": get_docker_images(),
        "active_runners": active_runners_list,
        "active_runner_count": len(active_runners_list),
        "is_busy": len(active_runners_list) > 0,
        "role": ROLE,
        "is_headnode": bool(ROLE.lower() in ("headnode", "headnode_worker"))
    }

    if caps["is_headnode"]:
        try:
            from src.runner.host_guard import get_headnode_safe_capacities
            caps = get_headnode_safe_capacities(caps)
        except ImportError:
            pass

    return caps

def build_registration_payload(is_startup=False):
    """Builds the heartbeat/registration JSON payload for the headnode,
    retaining all existing fields for backward compatibility while providing
    all new Cluster-CI v3 capacity fields (cpus, ram_gb, vram_per_gpu, unified_memory, arch, disk_free_gb,
    docker_images, active_runners, role, is_headnode).
    """
    caps = get_worker_capabilities()

    return {
        # Existing backward-compatible fields:
        "worker_id": WORKER_ID,
        "hostname": HOSTNAME,
        "service_url": SERVICE_URL,
        "total_ram_gb": caps["total_ram_gb"],
        "available_ram_gb": caps["available_ram_gb"],
        "total_storage_gb": caps["total_storage_gb"],
        "available_storage_gb": caps["available_storage_gb"],
        "total_vram_gb": caps["total_vram_gb"],
        "gpu_count": caps["gpu_count"],
        "gpu_name": caps["gpu_name"],
        "available_vram_gb": caps["available_vram_gb"],
        "is_startup": is_startup,

        # New Cluster-CI v3 capacity fields:
        "cpus": caps["cpus"],
        "ram_gb": caps["ram_gb"],
        "vram_per_gpu": caps["vram_per_gpu"],
        "unified_memory": caps["unified_memory"],
        "arch": caps["arch"],
        "disk_free_gb": caps["disk_free_gb"],
        "docker_images": caps["docker_images"],
        "active_runners": caps["active_runners"],
        "active_runner_count": caps["active_runner_count"],
        "role": caps.get("role", ROLE),
        "is_headnode": caps.get("is_headnode", False)
    }

def heartbeat_loop():
    is_startup = True
    while True:
        try:
            payload = build_registration_payload(is_startup=is_startup)
            resp = requests.post(f"{HEADNODE_URL}/register_worker", json=payload, headers=get_headers(), timeout=10)
            resp.raise_for_status()
            is_startup = False
            startup_heartbeat_event.set()
        except Exception as e:
            logger.error(f"Failed to send heartbeat: {e}")
        time.sleep(10)

def poll_for_job():
    try:
        resp = requests.get(f"{HEADNODE_URL}/worker_poll/{WORKER_ID}", headers=get_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("job_id"):
            return data
    except Exception as e:
        logger.error(f"Failed to poll: {e}")
    return None

def update_job_status(job_id, status, exit_code=None, commit_hash=None, viewer_port=None, runner_id=None, worker_id=None):
    payload = {"job_id": job_id, "status": status}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if commit_hash is not None:
        payload["commit_hash"] = commit_hash
    if viewer_port is not None:
        payload["viewer_port"] = viewer_port
    if runner_id is not None:
        payload["runner_id"] = runner_id
    if worker_id is not None:
        payload["worker_id"] = worker_id

    delay = 5
    max_attempts = 7  # 1 initial attempt + up to 6 retries
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(f"{HEADNODE_URL}/update_job_status", json=payload, headers=get_headers(), timeout=10)
            resp.raise_for_status()
            if attempt > 1:
                logger.info(f"Successfully updated job status to '{status}' on attempt {attempt}")
            return
        except Exception as e:
            if attempt < max_attempts:
                logger.warning(
                    f"Attempt {attempt}/{max_attempts} failed to update job status to '{status}' for job {job_id}: {e}. "
                    f"Retrying in {delay} seconds..."
                )
                time.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    f"❌ CRITICAL: All {max_attempts} attempts failed to update job status to '{status}' for job {job_id}: {e}"
                )
                raise

def execute_job(job):
    global current_job_id, current_process
    job_id = job['job_id']
    repo = job['repo']
    branch = job['branch']
    ram_limit_gb = job.get('ram_required_gb', DEFAULT_RAM_GB)
    max_runtime_hours = job.get('max_runtime_hours')
    p2p_url = job.get('p2p_url')
    gh_token = job.get('gh_token')
    env_vars = job.get('env_vars')

    # Detect parallel mode
    is_parallel = bool(
        job.get('parallel_mode') in (1, '1', True)
        or job.get('role') in ('executor', 'home_executor', 'additional_executor')
        or job.get('executor_role')
        or job.get('is_parallel')
    )

    runner_id = job.get('runner_id')
    if is_parallel and not runner_id:
        runner_id = f"runner-{WORKER_ID}-{uuid.uuid4().hex[:8]}"
    elif not runner_id:
        runner_id = f"classic-{job_id}"

    logger.info(f"Executing job {job_id} (runner_id={runner_id}, parallel_mode={is_parallel}) for {repo}@{branch} with {ram_limit_gb}GB limit")
    purge_orphan_runners_and_containers(job_id)
    update_job_status(job_id, 'running', runner_id=runner_id, worker_id=WORKER_ID)

    if not is_parallel:
        with job_lock:
            conflicting = [
                ex for ex in active_executors.values()
                if ex.get("repo") == repo and not ex.get("is_parallel") and ex.get("runner_id") != runner_id
            ]
            if conflicting:
                logger.warning(
                    f"⚠️ Workspace concurrency constraint: Classic job {job_id} shares single workspace 'repositories/{repo}' "
                    f"with active classic executor(s) {[c.get('runner_id') for c in conflicting]}. "
                    f"Classic jobs do not have isolated workspaces per runner like v3 (W2)."
                )

    with job_lock:
        active_executors[runner_id] = {
            "runner_id": runner_id,
            "job_id": job_id,
            "repo": repo,
            "branch": branch,
            "is_parallel": is_parallel,
            "start_time": time.time(),
            "process": None
        }
        current_job_id = job_id

    # We call the cluster-ci-run command which is supposed to be in /usr/local/bin/cluster-ci-run
    # or provided via CLUSTER_CI_RUN_PATH environment variable
    executable = os.environ.get("CLUSTER_CI_RUN_PATH")
    if not executable:
        if os.path.exists("/usr/local/bin/cluster-ci-run"):
            executable = "/usr/local/bin/cluster-ci-run"
        else:
            executable = os.path.join(BASE_DIR, "src", "runner", "run_research_pipeline.sh")
    cmd = [executable, repo, branch]

    env = os.environ.copy()
    env["CLUSTER_CI_MODE"] = "executor"
    env["JOB_ID"] = job_id
    env["LOGS_DIR"] = LOGS_DIR
    env["IS_LOCAL"] = "1" if job.get("is_local") else "0"
    if job.get('is_local'):
        logger.info(f"Injecting IS_LOCAL=1 for job {job_id}")
    workspace_key = "_local/" + repo if job.get("is_local") else repo
    commit_hash = job.get('commit_hash')
    if commit_hash:
        logger.info(f"Injecting CALLER_COMMIT_SHA for job {job_id}: {commit_hash}")
        env["CALLER_COMMIT_SHA"] = commit_hash
    if p2p_url:
        logger.info(f"Injecting P2P URL for job {job_id}: {p2p_url}")
        env["DVC_REMOTE_P2P_URL"] = p2p_url
    if gh_token:
        logger.info(f"Injecting GH_TOKEN for job {job_id}")
        env["GH_TOKEN"] = gh_token

    if is_parallel:
        env["CLUSTER_CI_PARALLEL_MODE"] = "1"
        env["CLUSTER_CI_RUNNER_ID"] = runner_id
        env["CLUSTER_CI_JOB_ID"] = job_id
        env["HEADNODE_URL"] = HEADNODE_URL
        env["CLUSTER_CI_HEADNODE_URL"] = HEADNODE_URL
        env["CLUSTER_CI_WORKER_ID"] = WORKER_ID
        if job.get("role"):
            env["CLUSTER_CI_ROLE"] = str(job["role"])
        if job.get("executor_role"):
            env["CLUSTER_CI_EXECUTOR_ROLE"] = str(job["executor_role"])
        logger.info(f"Parallel mode configured: runner_id={runner_id}, job_id={job_id}, headnode={HEADNODE_URL}")

    secrets_file = None
    if env_vars:
        try:
            parsed_vars = json.loads(env_vars) if isinstance(env_vars, str) else env_vars
            if parsed_vars:
                # Create a secure temp file for job secrets
                fd, secrets_file = tempfile.mkstemp(prefix=f"job_secrets_{job_id}_", suffix=".env")
                with os.fdopen(fd, 'w') as f:
                    for k, v in parsed_vars.items():
                        f.write(f"{k}={v}\n")
                logger.info(f"Injecting {len(parsed_vars)} custom environment variables via {secrets_file}")
                env["CLUSTER_CI_SECRETS_FILE"] = secrets_file
        except Exception as e:
            logger.error(f"Failed to write job secrets: {e}")

    log_path = os.path.join(LOGS_DIR, f"{job_id}.log")
    log_file = open(log_path, 'w', encoding='utf-8')

    # Log cancellation notifications if any previous runs were cancelled by this submission
    if env_vars:
        try:
            parsed_vars = json.loads(env_vars) if isinstance(env_vars, str) else env_vars
            if parsed_vars and "CLUSTER_CANCELLED_RUNS" in parsed_vars:
                cancelled_runs = [r.strip() for r in parsed_vars["CLUSTER_CANCELLED_RUNS"].split(",") if r.strip()]
                for cr_id in cancelled_runs:
                    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    log_file.write(f"[{now_str}] ℹ️  Previous active run [{cr_id}] has been cancelled by this new submission.\n")
                log_file.flush()
        except Exception as e:
            logger.error(f"Failed to write cancellation notifications to log: {e}")

    try:
        # Delete stale port file from previous runs
        port_file = os.path.join(REPOS_DIR, workspace_key, ".cluster-ci-viewer-port")
        if os.path.exists(port_file):
            try:
                os.remove(port_file)
            except Exception as e:
                logger.warning(f"Could not remove stale port file {port_file}: {e}")

        process = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        with job_lock:
            if runner_id in active_executors:
                active_executors[runner_id]["process"] = process
            current_process = process

        # Launch an unbuffered line-by-line real-time log streamer thread
        import threading
        def log_streamer():
            try:
                for line in process.stdout:
                    log_file.write(line)
                    log_file.flush()
                    try:
                        os.fsync(log_file.fileno())
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Error in log streamer: {e}")

        streamer_thread = threading.Thread(target=log_streamer, daemon=True)
        streamer_thread.start()

        port_reported = False
        start_time = time.time()
        timeout_seconds = (max_runtime_hours * 3600) if max_runtime_hours else (24 * 3600)

        # Status monitoring loop
        last_db_check = time.time()
        while process.poll() is None:
            time.sleep(2)
            
            # 1. Watchdog: Check for timeout
            elapsed = time.time() - start_time
            if elapsed > timeout_seconds:
                logger.error(f"❌ [WATCHDOG] Job {job_id} exceeded its {max_runtime_hours}h limit. Triggering forced destruction.")
                error_msg = f"\n❌ [CLUSTER WATCHDOG] Job exceeded maximum runtime of {max_runtime_hours} hours. Terminating.\n"
                log_file.write(error_msg)
                log_file.flush()

                # Inconditional destruction
                safe_job_id = job_id.replace('/', '-')
                safe_docker_rm_f([f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"], timeout=8)
                
                # Kill process tree on host
                try:
                    parent = psutil.Process(process.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
                
                process.terminate()
                break

            # 2. Active Self-Healing Watchdog: check job status on headnode every 10 seconds
            # to robustly detect cancellations from GitHub or Headnode DB even if Flask webhook fails.
            if time.time() - last_db_check > 10:
                last_db_check = time.time()
                try:
                    resp = requests.get(f"{HEADNODE_URL}/job_status/{job_id}", headers=get_headers(), timeout=5)
                    if resp.status_code == 200:
                        job_db = resp.json()
                        db_status = job_db.get("status")
                        if db_status not in ["running", "assigned"]:
                            logger.warning(f"⚠️ [SELF-HEALING] Active job {job_id} is marked as '{db_status}' in Headnode DB. Initiating instant local physical destruction!")
                            
                            # Physical destruction
                            safe_job_id = job_id.replace('/', '-')
                            safe_docker_rm_f([f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"], timeout=8)
                            
                            # Kill process tree on host
                            try:
                                parent = psutil.Process(process.pid)
                                for child in parent.children(recursive=True):
                                    try:
                                        child.kill()
                                    except psutil.NoSuchProcess:
                                        pass
                                parent.kill()
                            except psutil.NoSuchProcess:
                                pass
                                
                            purge_ollama_vram_on_host()
                            kill_dvc_viewer_processes()
                            break
                    elif resp.status_code == 404:
                        logger.warning(f"⚠️ [SELF-HEALING] Active job {job_id} not found in Headnode DB. Initiating instant local physical destruction!")
                        # Physical destruction
                        safe_job_id = job_id.replace('/', '-')
                        safe_docker_rm_f([f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"], timeout=8)
                        
                        # Kill process tree on host
                        try:
                            parent = psutil.Process(process.pid)
                            for child in parent.children(recursive=True):
                                try:
                                    child.kill()
                                except psutil.NoSuchProcess:
                                    pass
                            parent.kill()
                        except psutil.NoSuchProcess:
                            pass
                            
                        purge_ollama_vram_on_host()
                        kill_dvc_viewer_processes()
                        break
                except Exception as e:
                    logger.error(f"[SELF-HEALING] Failed to check job status on headnode: {e}")

            # Try to report dynamic viewer port if not already done
            if not port_reported:
                port_file = os.path.join(REPOS_DIR, workspace_key, ".cluster-ci-viewer-port")
                if os.path.exists(port_file):
                    try:
                        with open(port_file, 'r') as f:
                            viewer_port = int(f.read().strip())
                        logger.info(f"Reporting dynamic viewer port {viewer_port} for job {job_id}")
                        update_job_status(job_id, 'running', viewer_port=viewer_port)
                        port_reported = True
                    except Exception as e:
                        logger.error(f"Failed to read/report viewer port: {e}")

        exit_code = process.wait()
        streamer_thread.join(timeout=10)

        # Try to extract the commit hash from the job's directory
        commit_hash = None
        commit_file = os.path.join(REPOS_DIR, workspace_key, ".cluster-ci-commit")
        if os.path.exists(commit_file):
            try:
                with open(commit_file, 'r') as f:
                    commit_hash = f.read().strip()
            except Exception as e:
                logger.error(f"Failed to read commit hash file: {e}")

        if exit_code == 137:
            error_msg = f"❌ [CLUSTER INTERRUPTED] Execution interrupted (Exit code 137). This usually means an OOM (Out of Memory) or a Zombie Job Cleanup.\n"
            sys.stderr.write(error_msg)
            sys.stderr.flush()
            log_file.write(error_msg)
            try:
                res = subprocess.run("sudo dmesg -T | grep -i -E 'oom|kill' | tail -n 30", shell=True, capture_output=True, text=True)
                if res.stdout.strip():
                    log_file.write("\n--- SYSTEM DMESG (Kernel OOM Logs) ---\n")
                    log_file.write(res.stdout)
                    log_file.write("--------------------------------------\n")
            except:
                pass
            log_file.flush()
            update_job_status(job_id, 'failed', 137, commit_hash=commit_hash, runner_id=runner_id, worker_id=WORKER_ID)
        elif exit_code == 0:
            update_job_status(job_id, 'completed', exit_code, commit_hash=commit_hash, runner_id=runner_id, worker_id=WORKER_ID)
        elif exit_code < 0:
            # Likely killed by a signal (cancellation)
            logger.info(f"Job {job_id} was killed (exit code {exit_code})")
            update_job_status(job_id, 'failed', exit_code, commit_hash=commit_hash, runner_id=runner_id, worker_id=WORKER_ID)
        else:
            update_job_status(job_id, 'failed', exit_code, commit_hash=commit_hash, runner_id=runner_id, worker_id=WORKER_ID)

    except Exception as e:
        logger.error(f"Execution failed: {e}")
        try:
            update_job_status(job_id, 'failed', -1, runner_id=runner_id, worker_id=WORKER_ID)
        except Exception as update_err:
            logger.error(f"Failed to update failed job status to headnode: {update_err}")
    finally:
        # Inconditional, immediate physical cleanup of Docker containers & VRAM
        safe_job_id = job_id.replace('/', '-')
        try:
            safe_docker_rm_f([f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"], timeout=8)
        except Exception as docker_err:
            logger.error(f"Error purging job containers in finally: {docker_err}")
            
        try:
            purge_ollama_vram_on_host()
        except Exception as ollama_err:
            logger.error(f"Error purging Ollama VRAM in finally: {ollama_err}")

        kill_dvc_viewer_processes()
        if 'log_file' in locals() and not log_file.closed:
            log_file.close()
        with job_lock:
            active_executors.pop(runner_id, None)
            if active_executors:
                first_active = next(iter(active_executors.values()))
                current_job_id = first_active.get("job_id")
                current_process = first_active.get("process")
            else:
                current_job_id = None
                current_process = None
        if secrets_file and os.path.exists(secrets_file):
            try:
                os.remove(secrets_file)
                logger.info(f"Cleaned up secrets file: {secrets_file}")
            except Exception as e:
                logger.error(f"Failed to cleanup secrets file: {e}")

def drain_pending_syncs():
    logger.info("Starting drain of pending synchronizations...")

    # Path to registry.json
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    registry_path = os.path.join(base_dir, "repositories", "registry.json")

    logger.info(f"Looking for registry at: {registry_path}")
    if not os.path.exists(registry_path):
        logger.info("No registry.json found, nothing to drain.")
        return

    try:
        with open(registry_path, 'r') as f:
            registry = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load registry: {e}")
        return

    for project_name, data in registry.items():
        if project_name.startswith("_local/"):
            continue  # Local workspaces are evicted without uploading.
        if data.get("sync_status") == "pending":
            logger.info(f"Project {project_name} has pending sync. Checking headnode space...")
            try:
                resp = requests.get(f"{HEADNODE_URL}/check_space", timeout=5, headers=get_headers())
                resp.raise_for_status()
                space_info = resp.json()

                if space_info.get("sufficient"):
                    logger.info(f"Headnode space sufficient. Pushing {project_name}...")
                    project_dir = os.path.join(base_dir, "repositories", project_name)
                    if os.path.exists(project_dir):
                        # Check if a default DVC remote is configured
                        has_remote = False
                        dvc_config_path = os.path.join(project_dir, ".dvc", "config")
                        dvc_config_local_path = os.path.join(project_dir, ".dvc", "config.local")
                        
                        for config_path in [dvc_config_path, dvc_config_local_path]:
                            if os.path.exists(config_path):
                                with open(config_path, "r") as f:
                                    content = f.read()
                                    import re
                                    if re.search(r"^\s*remote\s*=", content, re.MULTILINE):
                                        has_remote = True
                                        break

                        if not has_remote:
                            logger.info(f"No default DVC remote configured for {project_name}. Skipping push.")
                            subprocess.run(["python3", os.path.join(base_dir, "src/runner/gc_orchestrator.py"), "mark-sync-done", project_name])
                        else:
                            # Execute dvc push via uv
                            res = subprocess.run(["uv", "run", "dvc", "push"], cwd=project_dir)
                            if res.returncode == 0:
                                # Mark as done
                                subprocess.run(["python3", os.path.join(base_dir, "src/runner/gc_orchestrator.py"), "mark-sync-done", project_name])
                                logger.info(f"Successfully pushed and marked {project_name} as done.")
                            else:
                                logger.error(f"dvc push failed for {project_name}")
                    else:
                        logger.warning(f"Project directory {project_dir} not found for {project_name}")
                else:
                    logger.info(f"Headnode still full ({space_info.get('free_gb'):.2f} GB free). Stopping drain.")
                    break
            except Exception as e:
                logger.error(f"Error during drain for {project_name}: {e}")

# Webhook server
app = Flask(__name__)

def valid_cluster_token():
    return bool(CLUSTER_TOKEN) and hmac.compare_digest(
        request.headers.get('Authorization', '').encode(),
        f'Bearer {CLUSTER_TOKEN}'.encode(),
    )


def workspace_path(repo, local=False):
    """Resolve a repository within its mode's namespace, including symlinks."""
    parts = repo.split('/')
    if len(parts) != 2 or any(p in {'', '.', '..', '_local'} for p in parts):
        abort(400)
    root = os.path.realpath(os.path.join(REPOS_DIR, '_local') if local else REPOS_DIR)
    path = os.path.realpath(os.path.join(root, repo))
    if os.path.commonpath([root, path]) != root:
        abort(400)
    if not local and os.path.commonpath([os.path.realpath(os.path.join(REPOS_DIR, '_local')), path]) == os.path.realpath(os.path.join(REPOS_DIR, '_local')):
        abort(400)
    return path


@app.before_request
def require_local_file_token():
    local = request.args.get('local') == '1'
    protected = request.endpoint == 'local_viewer_proxy'
    if request.endpoint in {'worker_dvc_get', 'worker_dvc_list', 'start_dvc_viewer'}:
        protected = protected or local
    if request.endpoint == 'fetch_artifact':
        path = os.path.realpath(os.path.join(REPOS_DIR, request.view_args['file_path']))
        relative = os.path.relpath(path, os.path.realpath(REPOS_DIR))
        protected = relative.split(os.sep)[0] in {'_local', '_local_uploads', '_local_results', '_local_transfers'}
    if protected and not valid_cluster_token():
        return jsonify({"error": "Unauthorized"}), 401


@app.route('/api/worker/local/view/<owner>/<repo>/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
@app.route('/api/worker/local/view/<owner>/<repo>/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def local_viewer_proxy(owner, repo, path):
    """Keep local viewers on loopback; expose them only through this token check."""
    root = workspace_path(f'{owner}/{repo}', local=True)
    try:
        with open(os.path.join(root, '.cluster-ci-viewer-port')) as handle:
            port = int(handle.read().strip())
        if not 1024 <= port <= 65535:
            abort(400)
        response = requests.request(
            request.method, f'http://127.0.0.1:{port}/{path}', params=request.args,
            data=request.get_data(), headers={'Content-Type': request.content_type} if request.content_type else {},
            stream=True, timeout=10, allow_redirects=False,
        )
        def generate():
            try:
                yield from response.iter_content(65536)
            finally:
                response.close()
        excluded = {'content-encoding', 'content-length', 'transfer-encoding', 'connection'}
        return Response(generate(), status=response.status_code, headers={
            k: v for k, v in response.headers.items() if k.lower() not in excluded
        })
    except (OSError, ValueError, requests.RequestException):
        return jsonify({"error": "Local viewer unavailable"}), 502


def _async_job_cleanup(job_id, safe_job_id, process_to_kill):
    """Background thread function to clean up Docker containers, purge host Ollama VRAM,
    terminate other viewer processes, and notify the headnode.
    """
    logger.info(f"🔄 [ASYNC CLEANUP] Starting background cleanup for job {job_id}")
    
    # 1. Kill host process tree of the runner process
    if process_to_kill:
        logger.info(f"🔄 [ASYNC CLEANUP] Killing runner process tree (PID: {process_to_kill.pid})")
        try:
            parent = psutil.Process(process_to_kill.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
            logger.info("✅ [ASYNC CLEANUP] Successfully killed runner process tree")
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            logger.error(f"❌ [ASYNC CLEANUP] Failed to kill runner process tree: {e}")
            
    # 2. Safe Docker Purge (Eradication + rm of all matching containers)
    containers_to_rm = [f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"]
    try:
        res = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=cluster-job-{safe_job_id}", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0 and res.stdout.strip():
            for c in res.stdout.strip().split("\n"):
                c = c.strip()
                if c and c not in containers_to_rm:
                    containers_to_rm.append(c)
    except Exception as e:
        logger.warning(f"Error querying docker containers for job {job_id}: {e}")
    safe_docker_rm_f(containers_to_rm, timeout=8)
    
    # 3. Purge host Ollama VRAM to instantly free Blackwell GPU physical memory
    purge_ollama_vram_on_host()
    
    # 4. Cleanup other viewers/processes
    kill_dvc_viewer_processes()
    
    # 5. Proactively update job status on headnode so DB is consistent immediately
    try:
        update_job_status(job_id, 'failed', exit_code=-15)
        logger.info(f"✅ [ASYNC CLEANUP] Successfully notified headnode that job {job_id} failed (-15)")
    except Exception as e:
        logger.error(f"❌ [ASYNC CLEANUP] Failed to update job status on headnode during cancellation: {e}")
        
    logger.info(f"✅ [ASYNC CLEANUP] Background cleanup complete for job {job_id}")

@app.route('/cancel/<target_id>', methods=['POST'])
@app.route('/cancel/runner/<target_id>', methods=['POST'])
def cancel_job(target_id):
    global current_job_id, current_process
    logger.info(f"Received cancellation request for target {target_id}")

    safe_target_id = target_id.replace('/', '-')
    matching_executors = []

    with job_lock:
        if target_id in active_executors:
            matching_executors.append(active_executors.pop(target_id))
        else:
            matching_keys = [k for k, ex in active_executors.items() if ex.get("job_id") == target_id]
            for k in matching_keys:
                matching_executors.append(active_executors.pop(k))

        # Backward compatibility for single-job mock/legacy state
        if not matching_executors and current_job_id == target_id:
            matching_executors.append({"job_id": target_id, "process": current_process})

        if active_executors:
            first_active = next(iter(active_executors.values()))
            current_job_id = first_active.get("job_id")
            current_process = first_active.get("process")
        else:
            current_job_id = None
            current_process = None

    for ex in matching_executors:
        j_id = ex.get("job_id", target_id)
        s_id = j_id.replace('/', '-')
        p_kill = ex.get("process")
        cleanup_thread = threading.Thread(
            target=_async_job_cleanup,
            args=(j_id, s_id, p_kill),
            daemon=True
        )
        cleanup_thread.start()

    if matching_executors:
        return jsonify({
            "status": "cancelled",
            "cancelled_runners": [ex.get("runner_id") for ex in matching_executors if ex.get("runner_id")],
            "message": "Cancellation initiated. Runner process tree and containers are being destroyed asynchronously in less than 5s."
        }), 200
    else:
        # Check if containers exist physically
        containers_exist = False
        try:
            res = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name=cluster-job-{safe_target_id}", "--filter", f"name=cluster-viewer-{safe_target_id}", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0 and res.stdout.strip():
                containers_exist = True
        except Exception:
            pass

        if containers_exist:
            cleanup_thread = threading.Thread(
                target=_async_job_cleanup,
                args=(target_id, safe_target_id, None),
                daemon=True
            )
            cleanup_thread.start()
            return jsonify({
                "status": "cancelled",
                "message": "Job not active in runner but matching containers found. Cancellation initiated asynchronously."
            }), 200
        else:
            return jsonify({
                "status": "not_found",
                "message": f"Job or runner '{target_id}' not active on this worker and no matching containers found"
            }), 404

@app.route('/job_logs/<job_id>', methods=['GET'])
def get_job_logs(job_id):
    offset = int(request.args.get('offset', 0))
    log_path = os.path.join(LOGS_DIR, f"{job_id}.log")
    
    if not os.path.exists(log_path):
        return jsonify({"logs": "", "offset": offset})
        
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(offset)
            new_logs = f.read()
            new_offset = f.tell()
        return jsonify({"logs": new_logs, "offset": new_offset})
    except Exception as e:
        logger.error(f"Error reading logs for {job_id}: {e}")
        return jsonify({"logs": "", "offset": offset}), 500

@app.route('/viewer_logs', methods=['GET'])
def get_viewer_logs():
    """Return the last 2000 chars of the dvc-viewer log file for diagnostics."""
    log_path = os.path.join(BASE_DIR, "dvc-viewer.log")
    if not os.path.exists(log_path):
        return jsonify({"logs": "No dvc-viewer.log found on this worker."})
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return jsonify({"logs": content[-2000:] if len(content) > 2000 else content})
    except Exception as e:
        return jsonify({"logs": f"Error reading dvc-viewer.log: {e}"}), 500

@app.route('/crash_report', methods=['GET'])
def get_crash_report():
    """Return recent kernel OOM/kill logs to help diagnose -98 errors."""
    try:
        res = subprocess.run(
            "sudo dmesg -T | grep -i -E 'oom|kill' | tail -n 50",
            shell=True, capture_output=True, text=True
        )
        syslog = subprocess.run(
            "sudo journalctl -u cluster-worker -n 50 --no-pager",
            shell=True, capture_output=True, text=True
        )
        return jsonify({
            "dmesg": res.stdout,
            "syslog": syslog.stdout
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/fetch_artifact/<path:file_path>', methods=['GET'])
def fetch_artifact(file_path):
    """
    Serves a file from the repositories directory.
    send_from_directory provides protection against directory traversal.
    """
    logger.info(f"Worker received request for artifact: {file_path}")
    return send_from_directory(REPOS_DIR, file_path)

def find_cas_file_in_repos(md5_hash):
    """
    Locates a CAS object by MD5 hash within any DVC repository cache under REPOS_DIR.
    Guarantees that the resolved path is strictly confined within REPOS_DIR.
    """
    clean_md5 = md5_hash.strip().lower()
    prefix = clean_md5[:2]
    suffix = clean_md5[2:]
    rel_cache = os.path.join(".dvc", "cache", "files", "md5", prefix, suffix)

    real_repos_dir = os.path.realpath(REPOS_DIR)

    def is_safe_and_file(target_path):
        if os.path.isfile(target_path):
            real_path = os.path.realpath(target_path)
            try:
                if os.path.commonpath([real_repos_dir, real_path]) == real_repos_dir:
                    return real_path
            except ValueError:
                return None
        return None

    # Check directly at root of REPOS_DIR if configured as a DVC root
    direct = is_safe_and_file(os.path.join(real_repos_dir, rel_cache))
    if direct:
        return direct

    # Scan up to 3 directory levels (repo, owner/repo, _local/owner/repo)
    try:
        for entry in os.scandir(real_repos_dir):
            if not entry.is_dir() or entry.name in {'.git'}:
                continue
            hit = is_safe_and_file(os.path.join(entry.path, rel_cache))
            if hit:
                return hit
            try:
                for sub in os.scandir(entry.path):
                    if not sub.is_dir() or sub.name in {'.git', '.dvc'}:
                        continue
                    hit = is_safe_and_file(os.path.join(sub.path, rel_cache))
                    if hit:
                        return hit
                    try:
                        for sub2 in os.scandir(sub.path):
                            if not sub2.is_dir() or sub2.name in {'.git', '.dvc'}:
                                continue
                            hit = is_safe_and_file(os.path.join(sub2.path, rel_cache))
                            if hit:
                                return hit
                    except (OSError, PermissionError):
                        continue
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        pass

    return None

@app.route('/fetch_cas/<md5>', methods=['GET'])
@app.route('/fetch_cas/<path:md5>', methods=['GET'])
def fetch_cas_object(md5):
    """
    Sert un objet CAS DVC par MD5 à travers les caches de dépôts sous REPOS_DIR (recommandation W6).
    Supporte les objets standards (32 caractères hexadécimaux) et les manifestes de répertoires (.dir).
    Sécurisé par validation regex stricte et confinement anti-traversal sous REPOS_DIR.
    """
    if not md5:
        return jsonify({"error": "Missing MD5"}), 400

    clean_md5 = md5.strip().lower()
    # Validation stricte MD5 : exactement 32 caractères hexadécimaux, optionnellement terminés par .dir
    if not re.match(r"^[0-9a-f]{32}(\.dir)?$", clean_md5):
        return jsonify({"error": "Invalid MD5 format"}), 400

    if ".." in clean_md5 or "/" in clean_md5 or "\\" in clean_md5:
        return jsonify({"error": "Path traversal characters forbidden"}), 400

    cas_file = find_cas_file_in_repos(clean_md5)
    if not cas_file:
        return jsonify({"error": f"CAS object '{clean_md5}' not found"}), 404

    logger.info(f"Serving CAS object {clean_md5} from {cas_file}")
    return send_file(cas_file, mimetype='application/octet-stream')

@app.route('/capabilities', methods=['GET'])
def worker_capabilities():
    """Returns worker capacities and state including hardware, capacity, docker images, and active runners."""
    return jsonify(get_worker_capabilities())

@app.route('/check_cache', methods=['POST'])
def check_cache():
    """
    Checks if the worker has the specified DVC cache files.
    Input JSON: {"repo": "owner/repo", "hashes": ["hash1", "hash2", ...]}
    Returns: JSON list of hashes present on this worker.
    """
    data = request.get_json()
    if not data or 'repo' not in data or 'hashes' not in data:
        return jsonify({"error": "Missing repo or hashes"}), 400

    repo = data['repo']
    hashes = data['hashes']
    found_hashes = []

    for h in hashes:
        if len(h) < 2:
            continue
        # DVC CAS nomenclature: .dvc/cache/files/md5/<2_chars>/<rest>
        cache_path = os.path.join(REPOS_DIR, repo, ".dvc", "cache", "files", "md5", h[:2], h[2:])
        if os.path.exists(cache_path):
            found_hashes.append(h)

    return jsonify(found_hashes)

def get_free_port():
    """Find a free TCP port on the host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port

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

def safe_cleanup_worktree(repo_path, worktree_dir, worktree_name=None):
    """Safely cleans up a git worktree with 4 defensive tiers:
    1. Unconditional unlock via git worktree unlock (and manual locked file removal if residual)
    2. Forceful git worktree remove (-f -f)
    3. git worktree prune --expire now
    4. Filesystem cleanup of worktree_dir and residual .git/worktrees/<name> metadata
    """
    if not worktree_name:
        worktree_name = os.path.basename(worktree_dir)

    logger.info(f"🧹 [WORKTREE CLEANUP] Cleaning up worktree '{worktree_name}' at {worktree_dir} in {repo_path}")

    # Tier 1: Inconditional Unlock
    try:
        subprocess.run(["git", "worktree", "unlock", worktree_name], cwd=repo_path, capture_output=True, timeout=10)
    except Exception as e:
        logger.debug(f"git worktree unlock {worktree_name} exception (ignored): {e}")

    try:
        subprocess.run(["git", "worktree", "unlock", worktree_dir], cwd=repo_path, capture_output=True, timeout=10)
    except Exception as e:
        logger.debug(f"git worktree unlock {worktree_dir} exception (ignored): {e}")

    # Manual lock file removal in .git/worktrees/<worktree_name>/locked
    git_dir = os.path.join(repo_path, ".git")
    if os.path.isdir(git_dir):
        locked_file = os.path.join(git_dir, "worktrees", worktree_name, "locked")
        if os.path.exists(locked_file):
            try:
                os.remove(locked_file)
                logger.info(f"Removed residual lock file {locked_file}")
            except Exception as e:
                logger.warning(f"Could not remove residual lock file {locked_file}: {e}")

    # Tier 2: Forceful worktree remove (-f -f)
    try:
        subprocess.run(["git", "worktree", "remove", "-f", "-f", worktree_dir], cwd=repo_path, capture_output=True, timeout=15)
    except Exception as e:
        logger.debug(f"git worktree remove -f -f {worktree_dir} exception (ignored): {e}")

    # Tier 3: Immediate worktree prune
    try:
        subprocess.run(["git", "worktree", "prune", "--expire", "now"], cwd=repo_path, capture_output=True, timeout=10)
    except Exception as e:
        logger.debug(f"git worktree prune exception (ignored): {e}")

    # Tier 4: Filesystem cleanup
    if os.path.exists(worktree_dir) or os.path.islink(worktree_dir) or os.path.lexists(worktree_dir):
        try:
            if os.path.islink(worktree_dir):
                os.unlink(worktree_dir)
            else:
                shutil.rmtree(worktree_dir, ignore_errors=True)
            logger.info(f"Removed worktree directory {worktree_dir}")
        except Exception as e:
            logger.warning(f"Could not remove worktree dir {worktree_dir}: {e}")

    if os.path.isdir(git_dir):
        metadata_dir = os.path.join(git_dir, "worktrees", worktree_name)
        if os.path.exists(metadata_dir):
            try:
                shutil.rmtree(metadata_dir, ignore_errors=True)
                logger.info(f"Removed residual git worktree metadata {metadata_dir}")
            except Exception as e:
                logger.warning(f"Could not remove git worktree metadata {metadata_dir}: {e}")


def prepare_dvc_worktree(repo_path, worktree_dir, target_rev):
    """Prepares an isolated worktree for DVC operations.

    1. Fast-Path: Checks if worktree already exists, is on the exact target commit SHA,
       and has a valid .dvc/cache symlink. If so, returns True immediately.
    2. Fallback: Completely cleans up any stale worktree state via safe_cleanup_worktree,
       creates the worktree via git worktree add --force --detach,
       securely configures the DVC cache (handling existing symlinks with os.path.islink/lexists),
       and runs a tolerant dvc checkout.
    """
    worktree_name = os.path.basename(worktree_dir)

    # 1. Fetch latest commits to ensure target_rev is known locally
    logger.info(f"Fetching latest commits for {repo_path}...")
    try:
        subprocess.run(["git", "fetch", "--all", "--prune"], cwd=repo_path, capture_output=True, timeout=30)
    except Exception as e:
        logger.warning(f"git fetch failed in {repo_path}: {e}")

    # Fast-Path Check
    try:
        if os.path.exists(worktree_dir) and os.path.isdir(worktree_dir):
            res_target = subprocess.run(
                ["git", "rev-parse", target_rev],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            res_current = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree_dir, capture_output=True, text=True, timeout=10
            )
            if res_target.returncode == 0 and res_current.returncode == 0:
                target_sha = res_target.stdout.strip()
                current_sha = res_current.stdout.strip()
                target_cache = os.path.join(worktree_dir, ".dvc", "cache")
                if target_sha == current_sha and (os.path.islink(target_cache) or os.path.exists(target_cache)):
                    logger.info(f"⚡ [FAST-PATH] Worktree at {worktree_dir} is already at commit {target_sha[:8]} with valid DVC cache. Reusing.")
                    return True
    except Exception as e:
        logger.debug(f"Fast-path validation encountered exception (proceeding to full recreate): {e}")

    # Fallback Path: Full clean + recreate
    logger.info(f"Creating isolated worktree at {worktree_dir} for revision {target_rev}...")
    safe_cleanup_worktree(repo_path, worktree_dir, worktree_name)

    res_wt = subprocess.run(
        ["git", "worktree", "add", "--force", "--detach", worktree_dir, target_rev],
        cwd=repo_path, capture_output=True, text=True, timeout=30
    )
    if res_wt.returncode != 0:
        logger.error(f"git worktree add failed: {res_wt.stderr.strip()}")
        raise RuntimeError(f"Failed to create worktree: {res_wt.stderr.strip()}")

    # Configure DVC cache
    worktree_dvc_dir = os.path.join(worktree_dir, ".dvc")
    os.makedirs(worktree_dvc_dir, exist_ok=True)

    source_cache = os.path.join(repo_path, ".dvc", "cache")
    target_cache = os.path.join(worktree_dvc_dir, "cache")

    if os.path.islink(target_cache) or os.path.lexists(target_cache):
        try:
            if os.path.islink(target_cache) or not os.path.isdir(target_cache):
                os.unlink(target_cache)
            else:
                shutil.rmtree(target_cache, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Could not remove stale DVC cache target link/dir: {e}")

    if os.path.exists(source_cache) and not os.path.exists(target_cache):
        try:
            os.symlink(source_cache, target_cache)
            logger.info(f"Symlinked DVC cache: {source_cache} -> {target_cache}")
        except Exception as e:
            logger.warning(f"Failed to symlink DVC cache: {e}")

    # Copy DVC config files so dvc checkout can locate the cache
    for config_file in ["config", "config.local"]:
        src = os.path.join(repo_path, ".dvc", config_file)
        dst = os.path.join(worktree_dvc_dir, config_file)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                logger.warning(f"Could not copy DVC config {config_file}: {e}")

    # Tolerant dvc checkout
    logger.info(f"Running dvc checkout in worktree for {repo_path}...")
    try:
        res_checkout = subprocess.run(
            [DVC_CMD, "checkout"], cwd=worktree_dir,
            capture_output=True, text=True, timeout=60
        )
        if res_checkout.returncode != 0:
            logger.warning(f"dvc checkout non-zero ({res_checkout.returncode}): {res_checkout.stderr.strip()}")
    except Exception as e:
        logger.warning(f"dvc checkout failed (non-fatal): {e}")

    return True


dvc_viewer_lock = threading.Lock()

@app.route('/api/worker/dvc-viewer/start', methods=['POST'])
def start_dvc_viewer():
    """Start an on-demand dvc-viewer historical instance on a free port.

    Uses a git worktree for isolation (avoids corrupting the main working directory)
    and symlinks the DVC cache for instant local checkout (no network required).
    Metrics/plots are in Git (cache: false), heavy outs are in .dvc/cache.
    Protected by dvc_viewer_lock for concurrency safety.
    """
    data = request.get_json() or {}
    repo = data.get('repo')
    rev = data.get('rev')
    if not repo:
        return jsonify({"error": "Missing 'repo' parameter"}), 400

    repo_path = workspace_path(repo, local=request.args.get('local') == '1')
    if not os.path.exists(repo_path):
        return jsonify({"error": f"Repository '{repo}' not found on this worker"}), 404

    # Deterministic worktree path: same repo+rev reuses the same directory
    repo_safe = repo.replace('/', '-')
    rev_short = (rev or 'main')[:12]
    worktree_name = f"dvc-viewer-{repo_safe}-{rev_short}"
    local = request.args.get('local') == '1'
    worktree_dir = repo_path if local else f"/tmp/{worktree_name}"
    target_rev = rev or "origin/main"

    with dvc_viewer_lock:
        proc = None
        try:
            # 1. Prepare worktree & DVC cache (with Fast-Path and defensive fallback)
            if local:
                try:
                    with open(os.path.join(repo_path, '.cluster-ci-viewer-port')) as handle:
                        existing_port = int(handle.read().strip())
                    with socket.create_connection(('127.0.0.1', existing_port), timeout=0.5):
                        return jsonify({'status': 'ok', 'port': existing_port})
                except (OSError, ValueError):
                    pass
            else:
                prepare_dvc_worktree(repo_path, worktree_dir, target_rev)

            # 2. Start dvc-viewer in the isolated worktree
            port = get_free_port()
            logger.info(f"Starting historical dvc-viewer for {repo} on port {port}")

            viewer_env = os.environ.copy()
            viewer_env["CLUSTER_CI_MODE"] = "executor"
            viewer_env["DVC_VIEWER_PROJECT_DIR"] = worktree_dir
            viewer_env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + viewer_env.get("PATH", "")

            dvc_viewer_bin = get_executable("dvc-viewer")
            cmd = [dvc_viewer_bin, "--port", str(port), "--host", "127.0.0.1" if request.args.get("local") == "1" else "0.0.0.0"]

            proc = subprocess.Popen(
                cmd,
                cwd=worktree_dir,
                env=viewer_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Robustly wait for the TCP port to be open and listening
            start_wait = time.time()
            port_open = False
            while time.time() - start_wait < 20:
                if proc.poll() is not None:
                    break

                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                try:
                    s.connect(('127.0.0.1', port))
                    port_open = True
                    s.close()
                    break
                except Exception:
                    pass
                time.sleep(0.5)

            if not port_open:
                logger.error(f"dvc-viewer failed to bind/open port {port} within 20 seconds")
                try:
                    proc.terminate()
                except Exception:
                    pass
                if not local:
                    safe_cleanup_worktree(repo_path, worktree_dir, worktree_name)
                return jsonify({"error": "dvc-viewer failed to start or open port"}), 500

            if local:
                with open(os.path.join(repo_path, '.cluster-ci-viewer-port'), 'w') as handle:
                    handle.write(str(port))
            logger.info(f"Historical dvc-viewer started for {repo} on port {port} (worktree: {worktree_dir})")
            return jsonify({"status": "ok", "port": port})

        except Exception as e:
            logger.error(f"Error starting historical dvc-viewer: {e}")
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
            if not local:
                safe_cleanup_worktree(repo_path, worktree_dir, worktree_name)
            return jsonify({"error": str(e)}), 500

@app.route('/api/worker/dvc/list', methods=['GET'])
def worker_dvc_list():
    repo = request.args.get('repo')
    rev = request.args.get('rev')
    if not repo: return jsonify({"error": "Missing repo"}), 400

    repo_path = workspace_path(repo, local=request.args.get('local') == '1')
    if not os.path.exists(repo_path):
        return jsonify({"error": "Repository not found on this worker"}), 404

    if request.args.get('local') == '1':
        directory = os.path.realpath(os.path.join(repo_path, request.args.get('path', '')))
        if os.path.commonpath([repo_path, directory]) != repo_path:
            abort(400)
        if not os.path.isdir(directory):
            return jsonify({"error": "Directory not found"}), 404
        files = []
        for entry in os.scandir(directory):
            if entry.name in {'.git', '.dvc'} or entry.is_symlink():
                continue
            files.append({'path': entry.name, 'is_dir': entry.is_dir(),
                          'size': entry.stat().st_size, 'isout': True})
        return jsonify(files)

    cmd = [DVC_CMD, "list", ".", "--dvc-only", "--json"]
    if rev: cmd += ["--rev", rev]

    try:
        res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
        if res.returncode == 0:
            return Response(res.stdout, mimetype='application/json')
        return jsonify({"error": res.stderr}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/worker/dvc/get', methods=['GET'])
def worker_dvc_get():
    repo = request.args.get('repo')
    rev = request.args.get('rev')
    file_path = request.args.get('path')
    if not repo or not file_path: return jsonify({"error": "Missing repo or path"}), 400

    repo_path = workspace_path(repo, local=request.args.get('local') == '1')
    if not os.path.exists(repo_path):
        return jsonify({"error": "Repository not found on this worker"}), 404

    resolved_file = os.path.realpath(os.path.join(repo_path, file_path))
    if os.path.commonpath([repo_path, resolved_file]) != repo_path:
        abort(400)
    if request.args.get('local') == '1':
        # Local pseudo revisions are run labels, not Git commits. Never fetch a remote.
        if os.path.isfile(resolved_file):
            return send_file(resolved_file, as_attachment=request.args.get('inline') != 'true')
        return jsonify({"error": "Local file not found"}), 404

    tmp_dir = tempfile.mkdtemp()
    try:
        # Ensure the requested revision is available locally
        if rev:
            subprocess.run(["git", "fetch", "origin"], cwd=repo_path,
                           capture_output=True, timeout=30)

        import mimetypes
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = 'application/octet-stream'
        disposition = "inline" if request.args.get("inline") == "true" else "attachment"

        # Strategy 1: DVC extraction at specific revision (historical integrity)
        cmd = [DVC_CMD, "get", ".", file_path, "--out", tmp_dir]
        if rev: cmd += ["--rev", rev]

        res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
        if res.returncode == 0:
            filename = os.path.basename(file_path)
            full_path = os.path.join(tmp_dir, filename)
            if os.path.exists(full_path) and os.path.isfile(full_path):
                def generate():
                    try:
                        with open(full_path, 'rb') as f:
                            while True:
                                chunk = f.read(4096)
                                if not chunk: break
                                yield chunk
                    finally:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                return Response(generate(), mimetype=mime_type,
                                headers={"Content-Disposition": f"{disposition}; filename=\"{filename}\""})

        shutil.rmtree(tmp_dir, ignore_errors=True)

        # Strategy 1.5: Git-tracked file extraction (metrics, plots, configs)
        # Files declared with `cache: false` in dvc.yaml live in Git, not DVC cache.
        # `dvc get` cannot retrieve them, but `git show` can at any revision.
        # We try multiple refs: the requested rev, origin/main (latest sync), and HEAD.
        subprocess.run(["git", "fetch", "origin", "main"], cwd=repo_path,
                       capture_output=True, timeout=15)
        refs_to_try = []
        if rev:
            refs_to_try.append(rev)
        refs_to_try.extend(["origin/main", "HEAD"])

        for ref in refs_to_try:
            try:
                res_git = subprocess.run(
                    ["git", "show", f"{ref}:{file_path}"],
                    cwd=repo_path, capture_output=True, timeout=10
                )
                if res_git.returncode == 0 and res_git.stdout:
                    ref_label = ref[:12] if len(ref) > 12 else ref
                    logger.info(f"[P2P] Serving git-tracked {file_path}@{ref_label} via git show")
                    filename = os.path.basename(file_path)
                    return Response(
                        res_git.stdout,
                        mimetype=mime_type,
                        headers={"Content-Disposition": f"{disposition}; filename=\"{filename}\""}
                    )
            except Exception as e:
                logger.warning(f"[P2P] git show fallback failed for {file_path}@{ref}: {e}")

        # Strategy 2: Direct filesystem fallback (P2P — file produced by dvc repro)
        # When no remote storage is configured, dvc get fails but the file
        # is already on disk from the last pipeline execution.
        direct_path = os.path.join(repo_path, file_path)
        if os.path.exists(direct_path) and os.path.isfile(direct_path):
            logger.info(f"[P2P] Serving {file_path} directly from working directory")
            return send_file(direct_path, as_attachment=(disposition == "attachment"),
                             mimetype=mime_type,
                             download_name=os.path.basename(file_path))

        # Strategy 3: DVC cache direct lookup via md5 hash from dvc.lock
        # When sync_metrics failed (e.g. OOM), the file is NOT in git and NOT
        # on the filesystem, but IS in the local DVC cache with its md5 hash.
        try:
            lock_path = os.path.join(repo_path, "dvc.lock")
            if os.path.exists(lock_path):
                import yaml as _yaml
                with open(lock_path, 'r') as lf:
                    lock_data = _yaml.safe_load(lf) or {}
                # Search all stages for the file path and its md5
                for stage_name, stage_data in lock_data.get("stages", {}).items():
                    for out_list_key in ("outs", "metrics", "plots"):
                        for out_entry in stage_data.get(out_list_key, []):
                            if out_entry.get("path") == file_path and out_entry.get("md5"):
                                md5 = out_entry["md5"]
                                cache_file = os.path.join(
                                    repo_path, ".dvc", "cache", "files", "md5",
                                    md5[:2], md5[2:]
                                )
                                if os.path.exists(cache_file):
                                    logger.info(f"[P2P] Serving {file_path} from DVC cache (md5={md5[:12]})")
                                    return send_file(
                                        cache_file,
                                        as_attachment=(disposition == "attachment"),
                                        mimetype=mime_type,
                                        download_name=os.path.basename(file_path)
                                    )
        except Exception as e:
            logger.warning(f"[P2P] DVC cache lookup failed for {file_path}: {e}")

        return jsonify({"error": f"File not found via DVC, git, filesystem, or cache: {file_path}"}), 404

    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": str(e)}), 500



@app.route('/webhook/drain_request', methods=['POST'])
def drain_request():
    logger.info("Received drain request webhook")
    # Run drain in a separate thread to avoid blocking the webhook response
    threading.Thread(target=drain_pending_syncs).start()
    return jsonify({"status": "accepted"})

def start_webhook_server():
    app.run(host='0.0.0.0', port=AGENT_PORT)

LOCK_FILE_PATH = os.environ.get("CLUSTER_WORKER_LOCK_PATH", os.path.join(tempfile.gettempdir(), "cluster-worker.lock"))
lock_file = None
shutdown_requested = False

def acquire_single_instance_lock():
    global lock_file
    try:
        lock_file = open(LOCK_FILE_PATH, "w")
        if os.name != 'nt':
            import fcntl
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                logger.error(f"❌ CRITICAL ERROR: Another instance of worker_agent.py is already running on this host (locked via {LOCK_FILE_PATH}). Exiting immediately to prevent conflict.")
                sys.exit(1)
        else:
            import msvcrt
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except (IOError, OSError):
                logger.error(f"❌ CRITICAL ERROR: Another instance of worker_agent.py is already running on this host (locked via {LOCK_FILE_PATH}). Exiting immediately to prevent conflict.")
                sys.exit(1)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        logger.info(f"Successfully acquired single instance lock on {LOCK_FILE_PATH} (PID: {os.getpid()})")
    except Exception as e:
        logger.error(f"Error while acquiring single instance lock: {e}")
        if isinstance(e, SystemExit):
            raise e
        sys.exit(1)

def release_single_instance_lock():
    global lock_file
    if lock_file:
        try:
            if os.name != 'nt':
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
            if os.path.exists(LOCK_FILE_PATH):
                os.remove(LOCK_FILE_PATH)
            logger.info("Released single instance lock.")
        except Exception as e:
            logger.error(f"Error releasing lock file: {e}")

def cleanup_active_jobs_and_containers():
    global current_job_id, current_process
    
    # Ensure residual host Ollama VRAM is freed instantly during active job cleanup
    purge_ollama_vram_on_host()
    
    with job_lock:
        if active_executors:
            for r_id, ex in list(active_executors.items()):
                j_id = ex.get("job_id")
                proc = ex.get("process")
                logger.warning(f"🧹 Initiating forced cleanup for active executor {r_id} (job {j_id}) due to shutdown request...")
                if j_id:
                    safe_job_id = str(j_id).replace('/', '-')
                    safe_docker_rm_f([f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"], timeout=8)
                if proc:
                    logger.info(f"Terminating local runner process (PID: {proc.pid}) tree...")
                    try:
                        parent = psutil.Process(proc.pid)
                        for child in parent.children(recursive=True):
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                        parent.kill()
                    except psutil.NoSuchProcess:
                        pass
                if j_id:
                    try:
                        logger.info(f"Notifying headnode of failure for job {j_id}...")
                        update_job_status(j_id, 'failed', exit_code=-15)
                    except Exception as e:
                        logger.error(f"Failed to update job status on shutdown for {j_id}: {e}")
            active_executors.clear()
            current_job_id = None
            current_process = None
        elif current_job_id:
            logger.warning(f"🧹 Initiating forced cleanup for active job {current_job_id} due to shutdown request...")
            safe_job_id = current_job_id.replace('/', '-')
            safe_docker_rm_f([f"cluster-job-{safe_job_id}", f"cluster-viewer-{safe_job_id}"], timeout=8)
            
            if current_process:
                logger.info(f"Terminating local runner process (PID: {current_process.pid}) tree...")
                try:
                    parent = psutil.Process(current_process.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            
            try:
                logger.info(f"Notifying headnode of failure for job {current_job_id}...")
                update_job_status(current_job_id, 'failed', exit_code=-15)
            except Exception as e:
                logger.error(f"Failed to update job status on shutdown: {e}")
            current_job_id = None
            current_process = None

    # Catch-all: kill ALL remaining cluster containers even if not tracked
    # This handles edge cases where current_job_id was lost (e.g. crash recovery)
    try:
        res = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=cluster-job-", "--filter", "name=cluster-viewer-"],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0 and res.stdout.strip():
            remaining = [c.strip() for c in res.stdout.strip().split("\n") if c.strip()]
            if remaining:
                logger.warning(f"🔥 Catch-all cleanup: {len(remaining)} untracked cluster container(s) found. Force-killing...")
                for container_id in remaining:
                    safe_docker_rm_f(container_id, timeout=8)
    except Exception as e:
        logger.error(f"Error in catch-all container cleanup: {e}")

def signal_handler(signum, frame):
    global shutdown_requested
    signame = signal.Signals(signum).name
    logger.warning(f"⚠️ Received shutdown signal {signame} ({signum}). Starting graceful shutdown sequence...")
    shutdown_requested = True
    
    try:
        cleanup_active_jobs_and_containers()
    except Exception as e:
        logger.error(f"Error during active jobs cleanup: {e}")
        
    try:
        release_single_instance_lock()
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")
        
    logger.info("Graceful shutdown sequence complete. Exiting process.")
    sys.exit(0)

def register_signals():
    for sig in [signal.SIGTERM, signal.SIGINT]:
        try:
            signal.signal(sig, signal_handler)
            logger.info(f"Registered signal handler for {signal.Signals(sig).name}")
        except ValueError:
            pass
    if hasattr(signal, 'SIGHUP'):
        try:
            signal.signal(signal.SIGHUP, signal_handler)
            logger.info("Registered signal handler for SIGHUP")
        except ValueError:
            pass

# Background self-healing loop has been retired and replaced by deterministic JIT purges at job execution and worker startup.

def main_loop():
    # Enforce single instance lock first
    acquire_single_instance_lock()
    
    # Register signal handling for graceful shutdown
    register_signals()

    # R4. Worker Startup Docker & Process Reconciliation
    logger.info("Executing startup JIT Docker and process reconciliation to clean up any orphan/zombie state...")
    try:
        purge_orphan_runners_and_containers()
    except Exception as e:
        logger.error(f"Failed to perform startup reconciliation: {e}")

    # Start webhook server in background thread
    threading.Thread(target=start_webhook_server, daemon=True).start()
    
    # Start heartbeat in background thread
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    # Wait for the first heartbeat to be processed before polling for jobs
    if not startup_heartbeat_event.wait(timeout=300):
        logger.error("Timeout: Failed to synchronize initial heartbeat with headnode after 5 minutes. Shutting down worker.")
        release_single_instance_lock()
        sys.exit(1)

    try:
        while not shutdown_requested:
            job = poll_for_job()
            if job:
                try:
                    t = threading.Thread(target=execute_job, args=(job,), daemon=True)
                    t.start()
                except Exception as e:
                    logger.error(f"❌ CRITICAL: Unhandled exception launching execute_job thread: {e}")
                    # Safety recovery to prevent locking down the worker
                    try:
                        purge_orphan_runners_and_containers()
                    except Exception as recovery_err:
                        logger.error(f"Failed to perform emergency recovery purge: {recovery_err}")
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt caught in main loop.")
    finally:
        release_single_instance_lock()

if __name__ == '__main__':
    main_loop()
