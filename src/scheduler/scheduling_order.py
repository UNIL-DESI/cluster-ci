"""
scheduling_order.py - Module unifié d'ordonnancement, de priorité et d'équité utilisateur.

Remplace queue_helper.py. Utilisé par :
- scheduler_loop.py (sélection des nœuds et des jobs, arbitrage)
- headnode_service.py (/api/workers/queues et affichage dashboard)

Règles d'ordonnancement unifiées :
1. Statut exécutable : ready (0) avant pending (1).
2. Priorité multi-niveaux : high (0) avant normal (1) avant low (2).
3. Équité utilisateur : un utilisateur détenant 0 machine physique attribuée
   passe avant un utilisateur qui en détient déjà (champ jobs.username).
4. FIFO : date de création du job (created_at ASC).
5. Affinité de conteneur chaud & profondeur DAG :
   - direct_child et same_image
   - same_image
   - direct_child
   - priority DAG DESC (-priority)
6. Nom du nœud ASC en cas d'égalité stricte.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PRIORITY_ORDER = {
    "high": 0,
    "normal": 1,
    "low": 2,
}


def get_priority_rank(priority: Optional[str]) -> int:
    """Retourne le rang numérique d'une priorité (0=high, 1=normal, 2=low)."""
    if not priority:
        return 1
    return PRIORITY_ORDER.get(str(priority).strip().lower(), 1)


def format_waiting_time(seconds: float) -> str:
    """Formate une durée d'attente en secondes sous forme lisible (ex. '42s', '5m 12s', '1h 00m')."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}s"
    m = s // 60
    rem_s = s % 60
    if m < 60:
        return f"{m}m {rem_s:02d}s" if rem_s else f"{m}m"
    h = m // 60
    rem_m = m % 60
    return f"{h}h {rem_m:02d}m"


def get_user_machine_counts(conn) -> Dict[str, int]:
    """Calcule le nombre de machines physiques distinctes (worker_id)

    actuellement attribuées ou occupées par chaque utilisateur (jobs.username).
    """
    cursor = conn.cursor()
    user_workers: Dict[str, set] = {}

    # 1. Nœuds en cours d'exécution dans job_nodes
    try:
        cursor.execute("""
            SELECT j.username, jn.worker_id
            FROM job_nodes jn
            JOIN jobs j ON jn.job_id = j.job_id
            WHERE jn.status = 'running' AND jn.worker_id IS NOT NULL
        """)
        for row in cursor.fetchall():
            u = row[0] or ""
            w = row[1]
            if w:
                user_workers.setdefault(u, set()).add(w)
    except Exception as e:
        logger.warning("Error fetching active worker counts from job_nodes: %s", e)

    # 2. Jobs classiques ou assignés dans jobs
    try:
        cursor.execute("""
            SELECT username, worker_id, home_worker, active_workers
            FROM jobs
            WHERE status IN ('assigned', 'running')
        """)
        for row in cursor.fetchall():
            u = row[0] or ""
            if row[1]:
                user_workers.setdefault(u, set()).add(row[1])
            if row[2]:
                user_workers.setdefault(u, set()).add(row[2])
            raw_act = row[3]
            if raw_act:
                try:
                    act_list = json.loads(raw_act) if isinstance(raw_act, str) else raw_act
                    for w in act_list:
                        user_workers.setdefault(u, set()).add(w)
                except Exception as ex:
                    logger.warning("Error parsing active_workers for user machine counts: %s", ex)
    except Exception as e:
        logger.warning("Error fetching active worker counts from jobs: %s", e)

    return {u: len(w_set) for u, w_set in user_workers.items()}


def scheduling_node_sort_key(
    node: Dict[str, Any],
    resources: Optional[Dict[str, Any]] = None,
    current_node_name: Optional[str] = None,
    current_image: Optional[str] = None,
    user_machine_counts: Optional[Dict[str, int]] = None,
) -> Tuple[int, int, int, str, int, int, int, float, str]:
    """Clé de tri canonique et unifiée pour la sélection des nœuds (ordre ascendant).

    1. Statut exécutable : ready (0) avant pending (1).
    2. Priorité : high (0) avant normal (1) avant low (2).
    3. Équité utilisateur : nombre de machines physiques occupées par l'utilisateur (0 avant >= 1).
    4. FIFO jobs : job_created_at ASC.
    5. Affinité conteneur chaud & profondeur DAG :
       - direct_child et same_image (0 avant 1)
       - same_image (0 avant 1)
       - direct_child (0 avant 1)
       - priority DESC (-priority dans le DAG)
    6. Nom du nœud ASC en cas d'égalité stricte.
    """
    status = node.get("status")
    status_rank = 0 if status == "ready" else 1

    # Résolution de la priorité
    res = resources or {}
    raw_prio = (
        node.get("scheduling_priority")
        or res.get("priority")
        or node.get("job_scheduling_priority")
        or "normal"
    )
    prio_rank = get_priority_rank(raw_prio)

    # Équité utilisateur
    username = str(node.get("username") or "")
    counts = user_machine_counts or {}
    user_equity_rank = counts.get(username, 0)

    # FIFO
    created_at = str(node.get("job_created_at") or node.get("created_at") or "")

    raw_deps = node.get("deps")
    deps = []
    if raw_deps:
        try:
            deps = json.loads(raw_deps) if isinstance(raw_deps, str) else raw_deps
        except Exception:
            deps = []

    is_direct_child = (current_node_name in deps) if current_node_name else False
    node_image = node.get("image") or res.get("image")
    same_image = (node_image == current_image) if current_image else False

    try:
        priority = float(node.get("priority", 0.0) or 0.0)
    except (ValueError, TypeError):
        priority = 0.0

    child_and_image_rank = 0 if (is_direct_child and same_image) else 1
    image_rank = 0 if same_image else 1
    child_rank = 0 if is_direct_child else 1
    priority_rank = -priority

    node_name = str(node.get("node_name") or "")

    return (
        status_rank,
        prio_rank,
        user_equity_rank,
        created_at,
        child_and_image_rank,
        image_rank,
        child_rank,
        priority_rank,
        node_name,
    )


def job_sort_key(
    job: Dict[str, Any],
    user_machine_counts: Optional[Dict[str, int]] = None,
) -> Tuple[int, int, str]:
    """Clé de tri canonique pour l'ordonnancement des jobs.

    1. Priorité job : high (0) avant normal (1) avant low (2).
    2. Équité utilisateur : nombre de machines physiques occupées (0 avant >= 1).
    3. FIFO : created_at ASC.
    """
    raw_prio = job.get("scheduling_priority") or "normal"
    prio_rank = get_priority_rank(raw_prio)
    username = str(job.get("username") or "")
    counts = user_machine_counts or {}
    user_equity_rank = counts.get(username, 0)
    created_at = str(job.get("created_at") or "")
    return (prio_rank, user_equity_rank, created_at)


def is_worker_eligible_for_node(
    worker: Dict[str, Any],
    node_resources: Dict[str, Any],
    allocated: Optional[Dict[str, Any]] = None,
) -> bool:
    """Vérifie l'éligibilité d'un worker pour exécuter un nœud donné."""
    try:
        from src.scheduler.scheduler_loop import is_worker_admissible_for_node
    except ImportError:
        from scheduler_loop import is_worker_admissible_for_node
    return is_worker_admissible_for_node(worker, node_resources, allocated=allocated)


# Alias pour rétrocompatibilité
scheduler_node_sort_key = scheduling_node_sort_key
is_worker_admissible_for_node = is_worker_eligible_for_node
