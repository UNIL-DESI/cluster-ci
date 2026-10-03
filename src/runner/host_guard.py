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

DEFAULT_CONTAINER_OOM_SCORE_ADJ: int = 500  # Positive score: killed before host daemons
DEFAULT_CONTAINER_PIDS_LIMIT: int = 4096   # Anti-fork bomb guard
DEFAULT_RAM_MARGIN_GB: float = 0.0          # Extra margin if requested

PRIORITY_HEADNODE_LAST: int = 0
PRIORITY_DEDICATED_DISCRETE: int = 50
PRIORITY_DEDICATED_UNIFIED_GB10: int = 100
HEADNODE_HOSTNAMES = {"isipol09", "headnode"}

try:
    from src.config.defaults import should_enforce_node_memory_limit
except ImportError:
    def should_enforce_node_memory_limit(role: str) -> bool:
        return str(role or "").strip().lower() in ("headnode", "headnode_worker")


def is_headnode_host(host_profile: Dict[str, Any]) -> bool:
    """Determine whether the given host profile corresponds to a headnode machine.
    
    Checks explicit flags ('is_headnode', 'role'), hostname, and environment variables.
    """
    if host_profile.get("is_headnode") is True:
        return True
    
    role = str(host_profile.get("role", "")).strip().lower()
    if role in ("headnode", "headnode_worker", "master"):
        return True

    hostname = str(host_profile.get("hostname", "")).strip().lower()
    if hostname in HEADNODE_HOSTNAMES:
        return True

    # Check host env fallback if checking current local host
    if os.environ.get("CLUSTER_CI_ROLE", "").lower() == "headnode":
        return True
    if os.environ.get("HEADNODE_HOST", "").lower() in (hostname, "localhost", "127.0.0.1"):
        if hostname and hostname in HEADNODE_HOSTNAMES:
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
    
    Higher score means higher preference during worker dispatch.
    The headnode is always assigned the lowest priority (0) to ensure
    it is only chosen when no other capable worker is available (worker of last resort).
    """
    if is_headnode_host(host_profile):
        return PRIORITY_HEADNODE_LAST

    if is_unified_memory_host(host_profile):
        return PRIORITY_DEDICATED_UNIFIED_GB10

    return PRIORITY_DEDICATED_DISCRETE


def format_memory_value(gb: float) -> str:
    """Format gigabytes into a standard Docker memory string (e.g., '2g' or '2560m')."""
    # Round to 2 decimal places to avoid floating point imprecisions
    gb = round(gb, 2)
    if gb.is_integer():
        return f"{int(gb)}g"
    mb = int(math.ceil(gb * 1024))
    return f"{mb}m"


def docker_resource_args(
    host_profile: Dict[str, Any],
    node_resources: Optional[Dict[str, Any]] = None,
    *,
    headnode_ram_reserve_gb: float = DEFAULT_HEADNODE_RAM_RESERVE_GB,
    headnode_cpu_reserve: int = DEFAULT_HEADNODE_CPU_RESERVE,
    headnode_disk_reserve_gb: float = DEFAULT_HEADNODE_DISK_RESERVE_GB,
    oom_score_adj: int = DEFAULT_CONTAINER_OOM_SCORE_ADJ,
    pids_limit: int = DEFAULT_CONTAINER_PIDS_LIMIT,
    ram_margin_gb: float = DEFAULT_RAM_MARGIN_GB,
    enforce_node_memory_limit: Optional[bool] = None,
) -> List[str]:
    """Calculate docker run resource constraints for a job container.
    
    Args:
        host_profile: Dict with keys:
            - 'hostname': str
            - 'role': str ('headnode' or 'worker')
            - 'total_ram_gb' or 'ram_gb': float
            - 'cpus': int
            - 'disk_free_gb': float
            - 'unified_memory': bool/int
            - 'is_headnode': Optional[bool]
            - 'enforce_node_memory_limit': Optional[bool]
        node_resources: Dict with requested resources:
            - 'ram_gb': float (default 2.0)
            - 'vram_gb': float (default 0.0)
            - 'cpus': int (default 4)
            - 'storage_gb': float (default 0.0)
        headnode_ram_reserve_gb: RAM in GB reserved for host on headnode (default 16.0).
        headnode_cpu_reserve: Number of CPUs reserved for host on headnode (default 2).
        headnode_disk_reserve_gb: Disk in GB reserved for host on headnode (default 20.0).
        oom_score_adj: Docker oom-score-adj value (default +500).
        pids_limit: Maximum allowed PIDs per container (default 4096).
        ram_margin_gb: Additional safety RAM margin in GB (default 0.0).
        enforce_node_memory_limit: If None, resolved from ENFORCE_NODE_MEMORY_LIMIT by role.
            Default is True for headnode (strict defense), False for dedicated workers (GB10).

    Returns:
        List of CLI arguments for docker run, e.g.:
        ['--memory=2g', '--memory-swap=2g', '--memory-swappiness=0', '--oom-score-adj=500', '--cpus=4', '--pids-limit=4096']

    Raises:
        ValueError: If headnode cannot admit the job due to strict reserve violations.
    """
    if node_resources is None:
        node_resources = {}

    is_headnode = is_headnode_host(host_profile)
    unified = is_unified_memory_host(host_profile)

    # Resolve whether node memory limits should be enforced
    if enforce_node_memory_limit is None:
        if "enforce_node_memory_limit" in host_profile:
            enforce_node_memory_limit = bool(host_profile["enforce_node_memory_limit"])
        else:
            role_key = "headnode" if is_headnode else str(host_profile.get("role", "worker"))
            enforce_node_memory_limit = should_enforce_node_memory_limit(role_key)

    # 1. Total Host Resources
    host_ram = float(host_profile.get("total_ram_gb") or host_profile.get("ram_gb") or 125.0)
    host_cpus = int(host_profile.get("cpus") or 24)
    host_disk = float(host_profile.get("disk_free_gb") or 100.0)

    # 2. Extract Requested Resources
    req_ram = float(node_resources.get("ram_gb", 2.0))
    req_vram = float(node_resources.get("vram_gb", 0.0))
    req_cpus = int(node_resources.get("cpus", 4))
    req_disk = float(node_resources.get("storage_gb", 0.0))

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

    args: List[str] = []

    # 5. Assemble Docker Flags
    # If memory limit is enforced (default on headnode): strict cgroups + swap prevention
    if enforce_node_memory_limit:
        mem_str = format_memory_value(effective_mem)
        args.extend([
            f"--memory={mem_str}",
            f"--memory-swap={mem_str}",
            "--memory-swappiness=0",
        ])

    args.extend([
        f"--oom-score-adj={oom_score_adj}",
        f"--cpus={effective_cpus}",
        f"--pids-limit={pids_limit}",
    ])

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
        "--enforce-memory-limit",
        dest="enforce_memory_limit",
        action="store_true",
        default=None,
        help="Explicitly enforce container memory limits",
    )
    parser.add_argument(
        "--no-enforce-memory-limit",
        dest="enforce_memory_limit",
        action="store_false",
        help="Explicitly disable container memory limits",
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

    try:
        flags_str = docker_resource_args_string(
            host_profile,
            node_resources,
            enforce_node_memory_limit=args.enforce_memory_limit,
        )
        print(flags_str)
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
