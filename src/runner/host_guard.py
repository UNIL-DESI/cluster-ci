"""Host Guard Module (Cluster-CI v3 - Worker W11).

Protects host resources from containerized job workloads, with specialized
reinforcements for dual-role headnode (scheduler + worker of last resort)
and architecture-aware resource boundaries (unified memory on GB10 vs discrete GPUs).

Key Features:
1. docker_resource_args: Calculates strict container isolation flags
   (--memory, --memory-swap, --oom-score-adj, --cpus, --pids-limit).
   - On headnode: enforces total - reserve ceilings (RAM, CPU, disk).
   - On unified memory (NVIDIA GB10): enforces ram + vram pool coverage.
2. placement_priority: Orders workers so that the headnode is strictly
   the worker of LAST RESORT.
3. get_headnode_safe_capacities: Trims headnode reservations from capacity
   advertisements to prevent scheduler overcommit.
4. CLI helper: Allows bash scripts (e.g., run_research_pipeline.sh) to query
   resource flags easily.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

# Defaults & Constants
DEFAULT_HEADNODE_RAM_RESERVE_GB: float = 16.0
DEFAULT_HEADNODE_CPU_RESERVE: int = 2
DEFAULT_HEADNODE_DISK_RESERVE_GB: float = 20.0
DEFAULT_HEADNODE_CGROUP_PARENT: str = "/cluster-jobs"

DEFAULT_CONTAINER_OOM_SCORE_ADJ: int = 500  # Positive score: killed before host daemons
DEFAULT_CONTAINER_PIDS_LIMIT: int = 4096   # Anti-fork bomb guard
DEFAULT_RAM_MARGIN_GB: float = 0.0          # Extra margin if requested

PRIORITY_HEADNODE_LAST: int = 0
PRIORITY_DEDICATED_DISCRETE: int = 50

try:
    from src.config.defaults import (
        DEFAULT_HEADNODE_CGROUP_PARENT,
        DEFAULT_HEADNODE_CPU_RESERVE,
        DEFAULT_HEADNODE_DISK_RESERVE_GB,
        DEFAULT_HEADNODE_RAM_RESERVE_GB,
        DEFAULT_PLACEMENT_PRIORITY,
        HEADNODE_PLACEMENT_PRIORITY,
    )
except ImportError:
    DEFAULT_PLACEMENT_PRIORITY = 50
    HEADNODE_PLACEMENT_PRIORITY = 0
    DEFAULT_HEADNODE_RAM_RESERVE_GB = 16.0
    DEFAULT_HEADNODE_CPU_RESERVE = 2
    DEFAULT_HEADNODE_DISK_RESERVE_GB = 20.0
    DEFAULT_HEADNODE_CGROUP_PARENT = "/cluster-jobs"

# Backward compatibility alias
PRIORITY_HEADNODE_LAST = HEADNODE_PLACEMENT_PRIORITY
PRIORITY_DEFAULT_WORKER = DEFAULT_PLACEMENT_PRIORITY


def is_headnode_host(host_profile: Dict[str, Any]) -> bool:
    """Determine whether the given host profile corresponds to a headnode machine.
    
    Checks explicit flags ('is_headnode', 'role') and environment variables.
    Zero hardcoded hostnames or IPs (Amendement A14).
    """
    if host_profile.get("is_headnode") is True:
        return True
    if host_profile.get("is_headnode") is False:
        return False
    
    role = str(host_profile.get("role", "")).strip().lower()
    if role in ("headnode", "headnode_worker", "master"):
        return True
    if role and role not in ("headnode", "headnode_worker", "master"):
        return False

    # Check host env fallback if checking current local host (when role not specified)
    if os.environ.get("CLUSTER_CI_ROLE", "").strip().lower() in ("headnode", "headnode_worker"):
        return True
    if os.environ.get("IS_HEADNODE", "").strip() in ("1", "true", "yes"):
        return True

    return False



def is_unified_memory_host(host_profile: Dict[str, Any]) -> bool:
    """Determine whether the host utilizes unified memory (e.g. Grace-Blackwell GB10)."""
    val = host_profile.get("unified_memory")
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "unified")
    
    # Check GPU name or architecture heuristic if present
    gpu_name = str(host_profile.get("gpu_name", "")).upper()
    if "GB10" in gpu_name or "GRACE" in gpu_name:
        return True

    arch = str(host_profile.get("arch", "")).lower()
    if arch in ("aarch64", "arm64") and "NVIDIA" in gpu_name:
        return True

    return False


def placement_priority(host_profile: Dict[str, Any]) -> int:
    """Calculate scheduling placement priority for a host.
    
    Convention: Higher value = preferred first.
    Default:
      - Headnode: HEADNODE_PLACEMENT_PRIORITY (0) -> strictly worker of last resort.
      - All non-headnode workers: DEFAULT_PLACEMENT_PRIORITY (50).
    Override:
      - Host-level: 'placement_priority' or 'priority' key in host_profile.
      - Environment: CLUSTER_CI_PLACEMENT_PRIORITY (allows Henri to favor specific nodes).
    """
    # 1. Check explicit override in host_profile
    if "placement_priority" in host_profile and host_profile["placement_priority"] is not None:
        try:
            return int(host_profile["placement_priority"])
        except (ValueError, TypeError):
            pass

    if "priority" in host_profile and host_profile["priority"] is not None:
        try:
            return int(host_profile["priority"])
        except (ValueError, TypeError):
            pass

    # 2. Check environment variable override
    env_override = os.environ.get("CLUSTER_CI_PLACEMENT_PRIORITY")
    if env_override is not None and env_override.strip() != "":
        try:
            return int(env_override.strip())
        except ValueError:
            pass

    # 3. Headnode check: worker of last resort
    if is_headnode_host(host_profile):
        return HEADNODE_PLACEMENT_PRIORITY

    # 4. Standard default for all other machines
    return DEFAULT_PLACEMENT_PRIORITY


def format_memory_value(gb: float) -> str:
    """Format gigabytes into a standard Docker memory string (e.g., '2g' or '2560m')."""
    # Round to 2 decimal places to avoid floating point imprecisions
    gb = round(gb, 2)
    if gb.is_integer():
        return f"{int(gb)}g"
    mb = int(math.ceil(gb * 1024))
    return f"{mb}m"


def check_cgroup_memory_limit(cgroup_parent: str) -> Tuple[bool, str, Optional[int]]:
    """Verify that the cgroup exists and has an active memory limit.
    
    Checks cgroup v1 (/sys/fs/cgroup/memory/<path>/memory.limit_in_bytes)
    and cgroup v2 (/sys/fs/cgroup/<path>/memory.max).
    Returns (is_valid, reason, limit_bytes).
    """
    clean_parent = cgroup_parent.strip("/")
    
    # 1. Cgroup v1 (Ubuntu 20.04 / isipol09)
    v1_file = f"/sys/fs/cgroup/memory/{clean_parent}/memory.limit_in_bytes"
    if os.path.exists(v1_file):
        try:
            with open(v1_file, "r") as f:
                val = int(f.read().strip())
                # Kernel cgroup v1 unlimited is >= 9223372036854771712 (PAGE_COUNTER_MAX)
                if val >= 9000000000000000000 or val <= 0:
                    return False, f"cgroup v1 '{clean_parent}' existe mais n'a AUCUNE limite définie (illimité: {val})", val
                return True, "ok", val
        except Exception as e:
            return False, f"erreur de lecture cgroup v1: {e}", None

    # 2. Cgroup v2 (Unified hierarchy)
    v2_file = f"/sys/fs/cgroup/{clean_parent}/memory.max"
    if os.path.exists(v2_file):
        try:
            with open(v2_file, "r") as f:
                content = f.read().strip()
                if content == "max":
                    return False, f"cgroup v2 '{clean_parent}' existe mais n'a AUCUNE limite définie (memory.max='max')", None
                val = int(content)
                if val <= 0:
                    return False, f"cgroup v2 '{clean_parent}' limite invalide ({val})", val
                return True, "ok", val
        except Exception as e:
            return False, f"erreur de lecture cgroup v2: {e}", None

    return False, f"le cgroup parent '{clean_parent}' est introuvable sous /sys/fs/cgroup", None


def docker_resource_args(
    host_profile: Dict[str, Any],
    node_resources: Optional[Dict[str, Any]] = None,
    *,
    headnode_ram_reserve_gb: float = DEFAULT_HEADNODE_RAM_RESERVE_GB,
    headnode_cpu_reserve: int = DEFAULT_HEADNODE_CPU_RESERVE,
    headnode_disk_reserve_gb: float = DEFAULT_HEADNODE_DISK_RESERVE_GB,
    headnode_cgroup_parent: str = DEFAULT_HEADNODE_CGROUP_PARENT,
    oom_score_adj: int = DEFAULT_CONTAINER_OOM_SCORE_ADJ,
    pids_limit: int = DEFAULT_CONTAINER_PIDS_LIMIT,
    ram_margin_gb: float = DEFAULT_RAM_MARGIN_GB,
    **kwargs: Any,
) -> List[str]:
    """Calculate docker run resource constraints for a job container.
    
    Amendement A12:
    Per-container memory limits (--memory, --memory-swap, --memory-swappiness=0,
    --oom-score-adj=500, --cpus, --pids-limit=4096) are unconditionally enforced
    on ALL machines (non-configurable). Any stage under-declaring RAM crashes with OOMKilled.

    Amendement A11:
    On headnode, injects --cgroup-parent=/cluster-jobs to bound aggregate RAM of
    packing containers to (total - 16 GB).
    """
    if node_resources is None:
        node_resources = {}

    is_headnode = is_headnode_host(host_profile)
    unified = is_unified_memory_host(host_profile)

    # 1. Total Host Resources
    host_ram = float(host_profile.get("total_ram_gb") or host_profile.get("ram_gb") or 125.0)
    host_cpus = int(host_profile.get("cpus") or 24)
    host_disk = float(host_profile.get("disk_free_gb") or 100.0)

    # 2. Extract Requested Resources
    req_ram_val = node_resources.get("ram_gb")
    req_ram = float(req_ram_val if req_ram_val is not None else 2.0)

    req_vram_val = node_resources.get("vram_gb")
    req_vram = float(req_vram_val if req_vram_val is not None else 0.0)

    req_cpus_val = node_resources.get("cpus")
    req_cpus = int(req_cpus_val if req_cpus_val is not None else 4)

    req_disk_val = node_resources.get("storage_gb")
    req_disk = float(req_disk_val if req_disk_val is not None else 0.0)

    # 3. Memory Calculation (Unified Memory vs Discrete GPU)
    if unified:
        base_mem = req_ram + req_vram
    else:
        base_mem = req_ram

    effective_mem = base_mem + ram_margin_gb

    # 4. Enforce Ceilings & Reserves
    if is_headnode:
        max_allowed_ram = host_ram - headnode_ram_reserve_gb
        if max_allowed_ram <= 0:
            raise ValueError(
                f"Headnode total RAM ({host_ram:.1f}GB) is less than reserved RAM ({headnode_ram_reserve_gb:.1f}GB)."
            )
        if effective_mem > max_allowed_ram:
            raise ValueError(
                f"Requested memory ({effective_mem:.1f}GB) exceeds headnode safety ceiling "
                f"({max_allowed_ram:.1f}GB = total {host_ram:.1f}GB - reserve {headnode_ram_reserve_gb:.1f}GB)."
            )

        max_allowed_cpus = max(1, host_cpus - headnode_cpu_reserve)
        effective_cpus = min(req_cpus, max_allowed_cpus)

        if req_disk > 0 and (host_disk - req_disk) < headnode_disk_reserve_gb:
            raise ValueError(
                f"Requested disk storage ({req_disk:.1f}GB) violates headnode disk reserve "
                f"(available: {host_disk:.1f}GB, reserve: {headnode_disk_reserve_gb:.1f}GB)."
            )
    else:
        system_buffer = 8.0 if unified else 4.0
        max_allowed_ram = max(1.0, host_ram - system_buffer)
        if effective_mem > max_allowed_ram:
            effective_mem = max_allowed_ram

        effective_cpus = min(req_cpus, max(1, host_cpus))

    mem_str = format_memory_value(effective_mem)
    args: List[str] = [
        f"--memory={mem_str}",
        f"--memory-swap={mem_str}",
        "--memory-swappiness=0",
        f"--oom-score-adj={oom_score_adj}",
        f"--cpus={effective_cpus}",
        f"--pids-limit={pids_limit}",
    ]

    # 5. Headnode container packing ceiling via parent cgroup (Amendement A11)
    if is_headnode:
        cgroup_parent = host_profile.get("cgroup_parent") or headnode_cgroup_parent
        if cgroup_parent:
            check_cgroup = host_profile.get("verify_cgroup", True)
            if check_cgroup and (os.path.isdir("/sys/fs/cgroup") or host_profile.get("enforce_cgroup_check")):
                valid, reason, _ = check_cgroup_memory_limit(cgroup_parent)
                if not valid:
                    raise ValueError(
                        f"Refus de production --cgroup-parent={cgroup_parent} sur le headnode : {reason}.\n"
                        f"Cause : Le cgroup parent est absent ou sans limite mémoire active (protection fantôme).\n"
                        f"Remède : Créez et configurez la limite globale du cgroup (total - 16 Go) avant de lancer des conteneurs.\n"
                        f"Commande : sudo mkdir -p /sys/fs/cgroup/memory/{cgroup_parent.strip('/')} && "
                        f"echo $(( ($(grep MemTotal /proc/meminfo | awk '{{print $2}}') - 16777216) * 1024 )) | "
                        f"sudo tee /sys/fs/cgroup/memory/{cgroup_parent.strip('/')}/memory.limit_in_bytes"
                    )
            args.append(f"--cgroup-parent={cgroup_parent}")

    # 6. Shared memory (--shm-size)
    # PyTorch DataLoader avec workers et NeMo exigent une mémoire partagée adéquate (sinon Bus error)
    # Alloué à 25% de la RAM effective du conteneur (minimum 2 Go)
    shm_size_gb = max(2.0, round(effective_mem * 0.25, 2))
    shm_str = format_memory_value(shm_size_gb)
    args.append(f"--shm-size={shm_str}")

    # 7. GPU Allocation Flags (Amendement A16)
    if "gpus" in node_resources:
        req_gpus = int(node_resources["gpus"] or 0)
        if req_gpus == 0:
            import logging
            logging.getLogger("cluster_ci.host_guard").info("aucun GPU demandé (meta.cluster.gpus=0)")
        else:
            gpu_ids = node_resources.get("gpu_ids")
            if gpu_ids:
                if isinstance(gpu_ids, list):
                    ids_str = ",".join(str(g) for g in gpu_ids)
                else:
                    ids_str = str(gpu_ids).strip()
                args.append(f'--gpus="device={ids_str}"')
            else:
                raise ValueError(f"req_gpus={req_gpus} requested but no gpu_ids assigned")

    return args


def docker_resource_args_string(
    host_profile: Dict[str, Any],
    node_resources: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> str:
    """Helper returning the Docker flags as a single shell-escaped string."""
    args = docker_resource_args(host_profile, node_resources, **kwargs)
    return " ".join(args)


def get_headnode_safe_capacities(
    raw_capacities: Dict[str, Any],
    *,
    reserve_ram_gb: float = DEFAULT_HEADNODE_RAM_RESERVE_GB,
    reserve_cpus: int = DEFAULT_HEADNODE_CPU_RESERVE,
    reserve_disk_gb: float = DEFAULT_HEADNODE_DISK_RESERVE_GB,
) -> Dict[str, Any]:
    """Adjust capacity advertisement dictionary when running on a headnode.
    
    Subtracts host safety reserves from advertised capacity so the scheduler
    never attempts to schedule jobs exceeding the safe boundary.
    Also flags the worker with role='headnode' and placement_priority='last' (0).
    """
    safe = dict(raw_capacities)

    raw_ram = float(safe.get("ram_gb") or safe.get("total_ram_gb") or 0.0)
    safe_ram = max(0.0, raw_ram - reserve_ram_gb)
    safe["ram_gb"] = round(safe_ram, 2)
    if "total_ram_gb" in safe:
        safe["total_ram_gb"] = round(safe_ram, 2)
    if "available_ram_gb" in safe:
        raw_avail = float(safe["available_ram_gb"])
        safe["available_ram_gb"] = round(max(0.0, raw_avail - reserve_ram_gb), 2)

    raw_cpus = int(safe.get("cpus") or 1)
    safe["cpus"] = max(1, raw_cpus - reserve_cpus)

    raw_disk = float(safe.get("disk_free_gb") or safe.get("available_storage_gb") or 0.0)
    safe_disk = max(0.0, raw_disk - reserve_disk_gb)
    if "disk_free_gb" in safe:
        safe["disk_free_gb"] = round(safe_disk, 2)
    if "available_storage_gb" in safe:
        safe["available_storage_gb"] = round(safe_disk, 2)

    safe["role"] = "headnode"
    safe["is_headnode"] = True
    safe["placement_priority"] = PRIORITY_HEADNODE_LAST

    return safe


# CLI Entrypoint for Shell Pipelines
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cluster-CI Host Guard: Generate docker resource flags and check capacity."
    )
    parser.add_argument(
        "--host-profile",
        type=str,
        default=None,
        help="JSON string or file path containing host profile dict",
    )
    parser.add_argument(
        "--node-resources",
        type=str,
        default=None,
        help="JSON string or file path containing node resources dict",
    )
    parser.add_argument("--ram-gb", type=float, default=None, help="Direct RAM requested (GB)")
    parser.add_argument("--vram-gb", type=float, default=None, help="Direct VRAM requested (GB)")
    parser.add_argument("--cpus", type=int, default=None, help="Direct CPUs requested")
    parser.add_argument("--role", type=str, default=None, help="Host role (headnode|worker)")
    parser.add_argument(
        "--priority",
        action="store_true",
        help="Output integer placement priority instead of docker flags",
    )
    parser.add_argument(
        "--safe-capacities",
        action="store_true",
        help="Filter host profile into headnode-safe capacity JSON",
    )

    parser.add_argument(
        "--cgroup-parent",
        type=str,
        default=None,
        help="Docker cgroup parent slice/path (e.g. /cluster-jobs)",
    )
    parser.add_argument(
        "--enforce-memory-limit",
        dest="enforce_memory_limit",
        action="store_true",
        default=None,
        help="Explicitly enforce container memory limits (deprecated: limits are now unconditional)",
    )
    parser.add_argument(
        "--no-enforce-memory-limit",
        dest="enforce_memory_limit",
        action="store_false",
        help="Explicitly disable container memory limits (deprecated: limits are now unconditional)",
    )

    args = parser.parse_args()

    # Load host profile
    host_profile: Dict[str, Any] = {}
    if args.host_profile:
        if os.path.exists(args.host_profile):
            with open(args.host_profile, "r", encoding="utf-8") as f:
                host_profile = json.load(f)
        else:
            host_profile = json.loads(args.host_profile)

    if args.role:
        host_profile["role"] = args.role

    if args.priority:
        print(placement_priority(host_profile))
        return

    if args.safe_capacities:
        safe = get_headnode_safe_capacities(host_profile)
        print(json.dumps(safe, indent=2))
        return

    # Load node resources
    node_resources: Dict[str, Any] = {}
    if args.node_resources:
        if os.path.exists(args.node_resources):
            with open(args.node_resources, "r", encoding="utf-8") as f:
                node_resources = json.load(f)
        else:
            node_resources = json.loads(args.node_resources)

    if args.ram_gb is not None:
        node_resources["ram_gb"] = args.ram_gb
    if args.vram_gb is not None:
        node_resources["vram_gb"] = args.vram_gb
    if args.cpus is not None:
        node_resources["cpus"] = args.cpus

    kwargs: Dict[str, Any] = {}
    if args.cgroup_parent:
        kwargs["headnode_cgroup_parent"] = args.cgroup_parent

    try:
        flags_str = docker_resource_args_string(
            host_profile,
            node_resources,
            **kwargs,
        )
        print(flags_str)
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
