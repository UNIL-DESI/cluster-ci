"""Single source of truth for Cluster-CI v3 resource defaults and project overrides."""

import os
import re
from typing import Any, Dict, List, Optional

# A15 - Rétention pathologique de la base de données SQLite (Cluster-CI v3)
# La base n'est purgée qu'en cas de croissance pathologique (> 1 Go).
DB_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES = 1024 * 1024 * 1024  # 1 Go
DB_RETENTION_PATHOLOGICAL_DAYS = 365  # 365 jours

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
    """Parse .cluster-ci file in the project repository root for resource overrides."""
    ci_path = os.path.join(repo_path, ".cluster-ci")
    if not os.path.isfile(ci_path):
        return {}

    try:
        with open(ci_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        raise IOError(f"Failed to read .cluster-ci file at '{ci_path}': {e}") from e

    overrides: Dict[str, Any] = {}

    m_img = re.search(r'DOCKER_IMAGE\s*=\s*["\']?([^"\'\s#]+)["\']?', content)
    if m_img:
        overrides["image"] = m_img.group(1).strip()

    m_arm = re.search(r'DOCKER_IMAGE_ARM64\s*=\s*["\']?([^"\'\s#]+)["\']?', content)
    if m_arm:
        overrides["image_arm64"] = m_arm.group(1).strip()

    m_amd = re.search(r'DOCKER_IMAGE_AMD64\s*=\s*["\']?([^"\'\s#]+)["\']?', content)
    if m_amd:
        overrides["image_amd64"] = m_amd.group(1).strip()

    m_ram = re.search(r'REQUIRED_RAM\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    if m_ram:
        val = float(m_ram.group(1))
        overrides["ram_gb"] = int(val) if val.is_integer() else val
    else:
        m_ram_flag = re.search(r'--ram\s+(\d+(?:\.\d+)?)', content)
        if m_ram_flag:
            val = float(m_ram_flag.group(1))
            overrides["ram_gb"] = int(val) if val.is_integer() else val

    m_vram = re.search(r'REQUIRED_VRAM\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    if m_vram:
        val = float(m_vram.group(1))
        overrides["vram_gb"] = int(val) if val.is_integer() else val

    m_workers = re.search(r'ALLOWED_WORKERS\s*=\s*(.+)', content)
    if m_workers:
        raw = m_workers.group(1).split("#")[0].strip()
        workers = [w.strip() for w in raw.split(",") if w.strip()]
        if workers:
            overrides["workers"] = workers

    return overrides
