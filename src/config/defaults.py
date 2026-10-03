"""Single source of truth for Cluster-CI v3 resource defaults and project overrides."""

import os
import re
from typing import Any, Dict, List, Optional

# Default resource requirements for a stage if unspecified
DEFAULT_RESOURCES: Dict[str, Any] = {
    "image": "nvcr.io/nvidia/pytorch:26.05-py3",
    "image_arm64": None,
    "image_amd64": None,
    "cpus": 4,
    "ram_gb": 10,
    "vram_gb": 0,
    "storage_gb": 0,
    "workers": None,
}

ALLOWED_CLUSTER_KEYS = {
    "image",
    "image_arm64",
    "image_amd64",
    "cpus",
    "ram_gb",
    "vram_gb",
    "storage_gb",
    "workers",
}


def parse_project_cluster_ci(repo_path: str) -> Dict[str, Any]:
    """Parse .cluster-ci file in the project repository root for resource overrides.
    
    Reuses existing codebase patterns (regex matching on REQUIRED_RAM, REQUIRED_VRAM,
    ALLOWED_WORKERS, DOCKER_IMAGE, etc.).
    """
    ci_path = os.path.join(repo_path, ".cluster-ci")
    if not os.path.isfile(ci_path):
        return {}

    try:
        with open(ci_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        raise IOError(f"Failed to read .cluster-ci file at '{ci_path}': {e}") from e

    overrides: Dict[str, Any] = {}

    # DOCKER_IMAGE
    m_img = re.search(r'DOCKER_IMAGE\s*=\s*["\']?([^"\'\s#]+)["\']?', content)
    if m_img:
        overrides["image"] = m_img.group(1).strip()

    # DOCKER_IMAGE_ARM64
    m_arm = re.search(r'DOCKER_IMAGE_ARM64\s*=\s*["\']?([^"\'\s#]+)["\']?', content)
    if m_arm:
        overrides["image_arm64"] = m_arm.group(1).strip()

    # DOCKER_IMAGE_AMD64
    m_amd = re.search(r'DOCKER_IMAGE_AMD64\s*=\s*["\']?([^"\'\s#]+)["\']?', content)
    if m_amd:
        overrides["image_amd64"] = m_amd.group(1).strip()

    # REQUIRED_RAM or --ram
    m_ram = re.search(r'REQUIRED_RAM\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    if m_ram:
        val = float(m_ram.group(1))
        overrides["ram_gb"] = int(val) if val.is_integer() else val
    else:
        m_ram_flag = re.search(r'--ram\s+(\d+(?:\.\d+)?)', content)
        if m_ram_flag:
            val = float(m_ram_flag.group(1))
            overrides["ram_gb"] = int(val) if val.is_integer() else val

    # REQUIRED_VRAM
    m_vram = re.search(r'REQUIRED_VRAM\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    if m_vram:
        val = float(m_vram.group(1))
        overrides["vram_gb"] = int(val) if val.is_integer() else val

    # ALLOWED_WORKERS
    m_workers = re.search(r'ALLOWED_WORKERS\s*=\s*(.+)', content)
    if m_workers:
        raw = m_workers.group(1).split("#")[0].strip()
        workers = [w.strip() for w in raw.split(",") if w.strip()]
        if workers:
            overrides["workers"] = workers

    return overrides


def validate_and_resolve_resources(
    stage_name: str,
    meta_cluster: Optional[Dict[str, Any]],
    project_overrides: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate meta.cluster fields and resolve resource hierarchy.
    
    Priority: meta.cluster > .cluster-ci project overrides > DEFAULT_RESOURCES.
    Raises ValueError / TypeError loudly on unknown keys or invalid types.
    """
    if meta_cluster is not None:
        if not isinstance(meta_cluster, dict):
            raise TypeError(
                f"Stage '{stage_name}': meta.cluster must be a dictionary, got {type(meta_cluster).__name__}"
            )
        # Check for unknown keys
        unknown_keys = set(meta_cluster.keys()) - ALLOWED_CLUSTER_KEYS
        if unknown_keys:
            raise ValueError(
                f"Stage '{stage_name}': unknown key(s) under meta.cluster: {sorted(unknown_keys)}. "
                f"Allowed keys are: {sorted(ALLOWED_CLUSTER_KEYS)}"
            )

        # Type validations
        for img_key in ("image", "image_arm64", "image_amd64"):
            if img_key in meta_cluster:
                val = meta_cluster[img_key]
                if val is not None and not isinstance(val, str):
                    raise TypeError(
                        f"Stage '{stage_name}': meta.cluster.{img_key} must be a string or null, got {type(val).__name__}"
                    )

        if "cpus" in meta_cluster:
            val = meta_cluster["cpus"]
            if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
                raise ValueError(
                    f"Stage '{stage_name}': meta.cluster.cpus must be a positive integer, got {val!r}"
                )

        for num_key in ("ram_gb", "vram_gb", "storage_gb"):
            if num_key in meta_cluster:
                val = meta_cluster[num_key]
                if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
                    raise ValueError(
                        f"Stage '{stage_name}': meta.cluster.{num_key} must be a non-negative number, got {val!r}"
                    )

        if "workers" in meta_cluster:
            val = meta_cluster["workers"]
            if val is not None:
                if not isinstance(val, list) or not all(isinstance(w, str) for w in val):
                    raise TypeError(
                        f"Stage '{stage_name}': meta.cluster.workers must be a list of string hostnames or null, got {val!r}"
                    )

    resolved: Dict[str, Any] = {}
    mc = meta_cluster or {}

    # Hierarchy: meta.cluster > project_overrides > DEFAULT_RESOURCES
    for key, def_val in DEFAULT_RESOURCES.items():
        if key in mc and mc[key] is not None:
            resolved[key] = mc[key]
        elif key in project_overrides and project_overrides[key] is not None:
            resolved[key] = project_overrides[key]
        else:
            resolved[key] = def_val

    return resolved


# Policy: Enforce container memory limit per node
# Default: Strictly enforced on headnode to protect master services from OOM.
# On dedicated workers (GB10, single executor per host under Amendement A6), disabled by default
# to avoid killing jobs (e.g. ECIR) that under-declare ram_gb while host RAM is abundant.
ENFORCE_NODE_MEMORY_LIMIT: Dict[str, bool] = {
    "headnode": True,
    "worker": False,
}


def should_enforce_node_memory_limit(role: str) -> bool:
    """Check if container memory limits should be enforced for a given role."""
    role_normalized = str(role or "worker").strip().lower()
    return ENFORCE_NODE_MEMORY_LIMIT.get(role_normalized, False)


# Scheduling: Worker placement priority defaults
# Convention: Higher value = preferred first.
# Default: All non-headnode machines share the same standard priority (50).
# Headnode is strictly the worker of last resort (0).
DEFAULT_PLACEMENT_PRIORITY: int = 50
HEADNODE_PLACEMENT_PRIORITY: int = 0

