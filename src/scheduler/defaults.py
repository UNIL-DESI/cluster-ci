"""
defaults.py - Paramètres opérationnels propres à l'ordonnanceur Cluster-CI v3.
Les ressources par défaut (DEFAULT_RESOURCES, ALLOWED_CLUSTER_KEYS) sont importées
directement depuis la source unique de vérité src.config.defaults (W1).
"""

try:
    from src.config.defaults import (
        DEFAULT_RESOURCES,
        ALLOWED_CLUSTER_KEYS,
        parse_project_cluster_ci,
        validate_and_resolve_resources,
    )
except ImportError:
    from config.defaults import (
        DEFAULT_RESOURCES,
        ALLOWED_CLUSTER_KEYS,
        parse_project_cluster_ci,
        validate_and_resolve_resources,
    )

# Alias dérivés directement de src.config.defaults (zéro duplication de constante de ressource en dur)
DEFAULT_DOCKER_IMAGE = DEFAULT_RESOURCES["image"]
DEFAULT_CPUS = DEFAULT_RESOURCES["cpus"]
DEFAULT_GPUS = DEFAULT_RESOURCES["gpus"]
DEFAULT_RAM_GB = float(DEFAULT_RESOURCES["ram_gb"])
DEFAULT_VRAM_GB = float(DEFAULT_RESOURCES["vram_gb"])
DEFAULT_STORAGE_GB = float(DEFAULT_RESOURCES["storage_gb"])
ALLOWED_RESOURCE_KEYS = ALLOWED_CLUSTER_KEYS

# Constantes opérationnelles propres à l'ordonnanceur Cluster-CI v3 (sans doublon)
ALLOW_PACKING: bool = True
OS_HEADROOM_GB: float = 8.0
RUNNER_HEARTBEAT_TIMEOUT_S: float = 60.0
RUNNER_HEARTBEAT_INTERVAL_S: float = 15.0
MAX_WORKERS_PER_JOB: int = 8

# Réserves de sécurité applicables au Headnode
HEADNODE_RAM_RESERVE_GB: float = 16.0
HEADNODE_CPU_RESERVE: int = 2

