"""Single source of truth for Cluster-CI v3 resource defaults and project overrides."""

import os
import re
from typing import Any, Dict, List, Optional

# A15 - Rétention pathologique de la base de données SQLite (Cluster-CI v3)
# La base n'est purgée qu'en cas de croissance pathologique (> 1 Go).
DB_RETENTION_PATHOLOGICAL_THRESHOLD_BYTES = 1024 * 1024 * 1024  # 1 Go
DB_RETENTION_PATHOLOGICAL_DAYS = 365  # 365 jours

# Default resource requirements for a stage if unspecified (amendments A16)
DEFAULT_RESOURCES: Dict[str, Any] = {
    "image": "nvcr.io/nvidia/pytorch:26.05-py3",
    "image_arm64": None,
    "image_amd64": None,
    "cpus": 2,
    "gpus": 0,
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
    "gpus",
    "ram_gb",
    "vram_gb",
    "storage_gb",
    "workers",
}


def parse_project_cluster_ci(repo_path: str) -> Dict[str, Any]:
    """Parse .cluster-ci file in the project repository root for resource overrides.
    
    Extracts default resources according to Cluster-CI v3 amendments A16:
    REQUIRED_CPUS, REQUIRED_GPUS, REQUIRED_RAM / --ram, REQUIRED_VRAM,
    REQUIRED_STORAGE / REQUIRED_DISK, DOCKER_IMAGE, DOCKER_IMAGE_ARM64,
    DOCKER_IMAGE_AMD64, ALLOWED_WORKERS.
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

    # REQUIRED_CPUS
    m_cpus = re.search(r'REQUIRED_CPUS\s*=\s*(\d+)', content)
    if m_cpus:
        overrides["cpus"] = int(m_cpus.group(1))

    # REQUIRED_GPUS
    m_gpus = re.search(r'REQUIRED_GPUS\s*=\s*(\d+)', content)
    if m_gpus:
        overrides["gpus"] = int(m_gpus.group(1))

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

    # If REQUIRED_VRAM > 0 is requested in .cluster-ci without specifying REQUIRED_GPUS, default gpus to 1
    if "gpus" not in overrides and overrides.get("vram_gb", 0) > 0:
        overrides["gpus"] = 1

    # REQUIRED_STORAGE or REQUIRED_DISK
    m_storage = re.search(r'(?:REQUIRED_STORAGE|REQUIRED_DISK)\s*=\s*(\d+(?:\.\d+)?)(?:GB|G)?', content)
    if m_storage:
        val = float(m_storage.group(1))
        overrides["storage_gb"] = int(val) if val.is_integer() else val

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
    Raises actionable ValueError / TypeError loudly on unknown keys, invalid types,
    or resource inconsistencies according to amendment A17.
    """
    if meta_cluster is not None:
        if not isinstance(meta_cluster, dict):
            raise TypeError(
                f"Fichier dvc.yaml, stage '{stage_name}' : 'meta.cluster' doit être un dictionnaire, reçu {type(meta_cluster).__name__}. "
                f"Cause : structure YAML non conforme sous meta.cluster. "
                f"Remède : définissez un objet clé-valeur sous 'cluster' dans dvc.yaml."
            )
        # Check for unknown keys
        unknown_keys = set(meta_cluster.keys()) - ALLOWED_CLUSTER_KEYS
        if unknown_keys:
            sorted_unk = sorted(unknown_keys)
            sorted_allowed = sorted(ALLOWED_CLUSTER_KEYS)
            raise ValueError(
                f"Fichier dvc.yaml, stage '{stage_name}' : clé(s) inconnue(s) sous 'meta.cluster' : {sorted_unk}. "
                f"Cause : la ou les clés indiquées ne font pas partie du schéma des ressources Cluster-CI v3. "
                f"Remède : modifiez ou supprimez cette clé sous meta.cluster dans dvc.yaml. "
                f"Clés valides autorisées : {sorted_allowed}."
            )

        # Type validations
        for img_key in ("image", "image_arm64", "image_amd64"):
            if img_key in meta_cluster:
                val = meta_cluster[img_key]
                if val is not None and not isinstance(val, str):
                    raise TypeError(
                        f"Fichier dvc.yaml, stage '{stage_name}' : type invalide pour 'meta.cluster.{img_key}' : {type(val).__name__}. "
                        f"Cause : {img_key} doit être une chaîne de caractères ou null. "
                        f"Remède : renseignez une URL d'image valide pour '{img_key}' sous meta.cluster dans dvc.yaml."
                    )

        if "cpus" in meta_cluster:
            val = meta_cluster["cpus"]
            if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
                raise ValueError(
                    f"Fichier dvc.yaml, stage '{stage_name}' : valeur invalide pour 'meta.cluster.cpus' : {val!r}. "
                    f"Cause : cpus doit être un entier strictement positif (>= 1). "
                    f"Remède : définissez un entier >= 1 pour 'cpus' sous meta.cluster dans dvc.yaml (défaut : 2)."
                )

        if "gpus" in meta_cluster:
            val = meta_cluster["gpus"]
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise ValueError(
                    f"Fichier dvc.yaml, stage '{stage_name}' : valeur invalide pour 'meta.cluster.gpus' : {val!r}. "
                    f"Cause : gpus doit être un entier positif ou nul (>= 0). "
                    f"Remède : définissez un entier >= 0 pour 'gpus' sous meta.cluster dans dvc.yaml (défaut : 0)."
                )

        for num_key in ("ram_gb", "vram_gb", "storage_gb"):
            if num_key in meta_cluster:
                val = meta_cluster[num_key]
                if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
                    raise ValueError(
                        f"Fichier dvc.yaml, stage '{stage_name}' : valeur invalide pour 'meta.cluster.{num_key}' : {val!r}. "
                        f"Cause : {num_key} doit être un nombre positif ou nul (>= 0). "
                        f"Remède : définissez un nombre >= 0 pour '{num_key}' sous meta.cluster dans dvc.yaml (défaut : {DEFAULT_RESOURCES[num_key]})."
                    )

        if "workers" in meta_cluster:
            val = meta_cluster["workers"]
            if val is not None:
                if not isinstance(val, list) or not all(isinstance(w, str) for w in val):
                    raise TypeError(
                        f"Fichier dvc.yaml, stage '{stage_name}' : type invalide pour 'meta.cluster.workers' : {val!r}. "
                        f"Cause : workers doit être une liste de noms d'hôtes (chaînes). "
                        f"Remède : définissez une liste de chaînes (ex: ['HEC45801']) pour 'workers' sous meta.cluster dans dvc.yaml."
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

    # Consistency rule (A16/A17): vram_gb > 0 requires gpus >= 1
    if resolved["vram_gb"] > 0 and resolved["gpus"] == 0:
        raise ValueError(
            f"Fichier dvc.yaml, stage '{stage_name}' : incohérence de ressources entre 'meta.cluster.vram_gb' "
            f"({resolved['vram_gb']} Go) et 'meta.cluster.gpus' (0). "
            f"Cause : vram_gb exige gpus >= 1 (la mémoire vidéo ne peut être allouée sans GPU). "
            f"Remède : déclarez 'gpus: 1' (ou plus) sous meta.cluster dans dvc.yaml (ou REQUIRED_GPUS dans .cluster-ci), "
            f"ou fixez vram_gb à 0."
        )

    return resolved


# Scheduling: Worker placement priority defaults (Amendement A13/A14)
# Convention: Higher value = preferred first.
# Default: All non-headnode machines share standard priority (50).
# Headnode is strictly the worker of last resort (0).
DEFAULT_PLACEMENT_PRIORITY: int = 50
HEADNODE_PLACEMENT_PRIORITY: int = 0

# Headnode Resource Reservation & Packing Ceilings (Amendement A11/A12/A14)
DEFAULT_HEADNODE_RAM_RESERVE_GB: float = 16.0
DEFAULT_HEADNODE_CPU_RESERVE: int = 2
DEFAULT_HEADNODE_DISK_RESERVE_GB: float = 20.0
DEFAULT_HEADNODE_CGROUP_PARENT: str = "/cluster-jobs"


