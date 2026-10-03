"""
defaults.py - Re-export pur de src.config.defaults (source unique de vérité v3).
Aucune duplication de constante.
"""

try:
    from src.config.defaults import *
except ImportError:
    from config.defaults import *

# Source unique pour les valeurs dérivées et constantes d'ordonnancement Cluster-CI v3
DEFAULT_DOCKER_IMAGE = DEFAULT_RESOURCES.get("image", "nvcr.io/nvidia/pytorch:26.05-py3")
DEFAULT_RAM_GB = float(DEFAULT_RESOURCES.get("ram_gb", 10.0))
DEFAULT_VRAM_GB = float(DEFAULT_RESOURCES.get("vram_gb", 0.0))
DEFAULT_CPUS = 2
DEFAULT_GPUS = 0
DEFAULT_STORAGE_GB = float(DEFAULT_RESOURCES.get("storage_gb", 0.0))
ALLOWED_RESOURCE_KEYS = ALLOWED_CLUSTER_KEYS | {"gpus"}

ALLOW_PACKING: bool = True
OS_HEADROOM_GB: float = 8.0
RUNNER_HEARTBEAT_TIMEOUT_S: float = 60.0
RUNNER_HEARTBEAT_INTERVAL_S: float = 15.0
MAX_WORKERS_PER_JOB: int = 8

HEADNODE_RAM_RESERVE_GB: float = 16.0
HEADNODE_CPU_RESERVE: int = 2
DEFAULT_HEADNODE_RAM_RESERVE_GB: float = 16.0
DEFAULT_HEADNODE_CPU_RESERVE: int = 2
DEFAULT_HEADNODE_DISK_RESERVE_GB: float = 20.0
DEFAULT_HEADNODE_CGROUP_PARENT: str = "/cluster-jobs"

DEFAULT_PLACEMENT_PRIORITY: int = 50
HEADNODE_PLACEMENT_PRIORITY: int = 0
