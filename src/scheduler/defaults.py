"""
defaults.py - Paramètres opérationnels propres à l'ordonnanceur Cluster-CI v3.
Source unique de vérité : src.config.defaults (W1 / W10 / W11 / W3).
Ce module réexporte les constantes pour compatibilité sans aucun doublon.
"""

try:
    from src.config.defaults import (
        DEFAULT_RESOURCES,
        ALLOWED_CLUSTER_KEYS,
        parse_project_cluster_ci,
        validate_and_resolve_resources,
        DEFAULT_DOCKER_IMAGE,
        DEFAULT_CPUS,
        DEFAULT_GPUS,
        DEFAULT_RAM_GB,
        DEFAULT_VRAM_GB,
        DEFAULT_STORAGE_GB,
        ALLOWED_RESOURCE_KEYS,
        ALLOW_PACKING,
        OS_HEADROOM_GB,
        RUNNER_HEARTBEAT_TIMEOUT_S,
        RUNNER_HEARTBEAT_INTERVAL_S,
        MAX_WORKERS_PER_JOB,
        HEADNODE_RAM_RESERVE_GB,
        HEADNODE_CPU_RESERVE,
        DEFAULT_PLACEMENT_PRIORITY,
        HEADNODE_PLACEMENT_PRIORITY,
        DEFAULT_HEADNODE_RAM_RESERVE_GB,
        DEFAULT_HEADNODE_CPU_RESERVE,
        DEFAULT_HEADNODE_DISK_RESERVE_GB,
        DEFAULT_HEADNODE_CGROUP_PARENT,
        DB_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES,
        DB_RETENTION_PATHOLOGICAL_DAYS,
    )
except ImportError:
    from config.defaults import (
        DEFAULT_RESOURCES,
        ALLOWED_CLUSTER_KEYS,
        parse_project_cluster_ci,
        validate_and_resolve_resources,
        DEFAULT_DOCKER_IMAGE,
        DEFAULT_CPUS,
        DEFAULT_GPUS,
        DEFAULT_RAM_GB,
        DEFAULT_VRAM_GB,
        DEFAULT_STORAGE_GB,
        ALLOWED_RESOURCE_KEYS,
        ALLOW_PACKING,
        OS_HEADROOM_GB,
        RUNNER_HEARTBEAT_TIMEOUT_S,
        RUNNER_HEARTBEAT_INTERVAL_S,
        MAX_WORKERS_PER_JOB,
        HEADNODE_RAM_RESERVE_GB,
        HEADNODE_CPU_RESERVE,
        DEFAULT_PLACEMENT_PRIORITY,
        HEADNODE_PLACEMENT_PRIORITY,
        DEFAULT_HEADNODE_RAM_RESERVE_GB,
        DEFAULT_HEADNODE_CPU_RESERVE,
        DEFAULT_HEADNODE_DISK_RESERVE_GB,
        DEFAULT_HEADNODE_CGROUP_PARENT,
        DB_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES,
        DB_RETENTION_PATHOLOGICAL_DAYS,
    )
