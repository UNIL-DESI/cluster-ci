"""Helper pour le tri et le formatage de la file d'attente par machine.

Ce module formalise la logique d'ordonnancement de scheduler_loop.py pour le dashboard et l'API :
- Jobs traités en FIFO strict selon jobs.created_at ASC (scheduler_loop.py:1568).
- Nœuds 'ready' prioritaires sur les nœuds 'pending' (les nœuds pending attendent la complétion de dépendances).
- Nœuds ordonnés selon la logique de node_sort_key (scheduler_loop.py:1109-1122) où priority (profondeur du DAG)
  est le critère déterminant en l'absence de conteneur d'exécution chaud (reverse=True -> plus haute priorité d'abord).
- Note architecturale : Dans scheduler_loop.py, node_sort_key est une fonction imbriquée privée à l'intérieur
  de handle_next_node (lignes 1109-1122). Conformément à la directive d'intégrité interdisant toute modification
  de scheduler_loop.py, cette logique est extraite ici en helper partagé en lecture seule.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple


def format_waiting_time(seconds: float) -> str:
    """Formate une durée d'attente en secondes sous forme lisible (ex.

    '42s', '5m 12s', '1h 00m').
    """
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


def scheduler_node_sort_key(
    node: Dict[str, Any],
    resources: Optional[Dict[str, Any]] = None,
    current_node_name: Optional[str] = None,
    current_image: Optional[str] = None,
) -> Tuple[int, str, int, int, int, float, str]:
    """Reproduit exactement la logique de sélection de scheduler_loop.py.

    Critères d'ordonnancement :
    1. Statut exécutable : ready (0) avant pending (1).
    2. FIFO jobs : job_created_at ASC.
    3. node_sort_key (scheduler_loop.py:1109-1122) inversé pour tri ascendant global :
       - direct_child et same_image (affinité conteneur chaud)
       - same_image
       - direct_child
       - priority DESC (profondeur dans le graphe DAG)
    4. Nom du nœud ASC en cas d'égalité stricte.
    """
    status = node.get("status")
    status_rank = 0 if status == "ready" else 1

    created_at = str(node.get("job_created_at") or node.get("created_at") or "")

    raw_deps = node.get("deps")
    deps = []
    if raw_deps:
        try:
            deps = json.loads(raw_deps) if isinstance(raw_deps, str) else raw_deps
        except Exception:
            deps = []

    is_direct_child = (current_node_name in deps) if current_node_name else False
    res = resources or {}
    node_image = node.get("image") or res.get("image")
    same_image = (node_image == current_image) if current_image else False

    try:
        priority = float(node.get("priority", 0.0) or 0.0)
    except (ValueError, TypeError):
        priority = 0.0

    # Inversion des indicateurs affinité/priorité pour tri ascendant
    child_and_image_rank = 0 if (is_direct_child and same_image) else 1
    image_rank = 0 if same_image else 1
    child_rank = 0 if is_direct_child else 1
    priority_rank = -priority

    node_name = str(node.get("node_name") or "")

    return (
        status_rank,
        created_at,
        child_and_image_rank,
        image_rank,
        child_rank,
        priority_rank,
        node_name,
    )
