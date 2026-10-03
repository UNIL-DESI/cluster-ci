"""
defaults.py - Pont de compatibilité vers src.config.defaults (livré par W1)
et constantes d'ordonnancement pour Cluster-CI v3.
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

# Images Docker par défaut
DEFAULT_DOCKER_IMAGE = DEFAULT_RESOURCES.get("image", "nvcr.io/nvidia/pytorch:26.05-py3")

# Dimensionnement des ressources par défaut d'un nœud
DEFAULT_RAM_GB = float(DEFAULT_RESOURCES.get("ram_gb", 10.0))
DEFAULT_VRAM_GB = float(DEFAULT_RESOURCES.get("vram_gb", 0.0))
DEFAULT_CPUS = int(DEFAULT_RESOURCES.get("cpus", 4))
DEFAULT_STORAGE_GB = float(DEFAULT_RESOURCES.get("storage_gb", 0.0))

# Politique de colocation (Amendement A6 : un seul exécuteur actif par machine en v3)
ALLOW_PACKING = False

# Marge système / OS headroom en Go (réserve ZFS, CUDA unifiée, Docker)
OS_HEADROOM_GB = 8.0

# Timeout de détection de mort d'un runner (secondes)
RUNNER_HEARTBEAT_TIMEOUT_S = 60.0

# Intervalle recommandé d'envoi des heartbeats par le runner (secondes)
RUNNER_HEARTBEAT_INTERVAL_S = 15.0

# Nombre maximal de workers allouables à un même job parallèle
MAX_WORKERS_PER_JOB = 8

# Clés de ressources autorisées dans la déclaration meta.cluster / nodes.resources
ALLOWED_RESOURCE_KEYS = ALLOWED_CLUSTER_KEYS

def get_node_defaults():
    """Renvoie un dictionnaire des ressources par défaut pour un nœud DVC."""
    return dict(DEFAULT_RESOURCES)
