"""
defaults.py - Valeurs par défaut canoniques et règles de dimensionnement pour Cluster-CI v3.

Source unique de vérité pour les défauts des nœuds et de l'ordonnanceur.
"""

# Images Docker par défaut
DEFAULT_DOCKER_IMAGE = "nvcr.io/nvidia/pytorch:26.05-py3"

# Dimensionnement des ressources par défaut d'un nœud
DEFAULT_RAM_GB = 10.0
DEFAULT_VRAM_GB = 0.0
DEFAULT_CPUS = 4
DEFAULT_STORAGE_GB = 0.0

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
ALLOWED_RESOURCE_KEYS = {
    "image",
    "image_arm64",
    "image_amd64",
    "cpus",
    "ram_gb",
    "vram_gb",
    "storage_gb",
    "workers"
}

def get_node_defaults():
    """Renvoie un dictionnaire des ressources par défaut pour un nœud DVC."""
    return {
        "image": DEFAULT_DOCKER_IMAGE,
        "image_arm64": None,
        "image_amd64": None,
        "cpus": DEFAULT_CPUS,
        "ram_gb": DEFAULT_RAM_GB,
        "vram_gb": DEFAULT_VRAM_GB,
        "storage_gb": DEFAULT_STORAGE_GB,
        "workers": None
    }
