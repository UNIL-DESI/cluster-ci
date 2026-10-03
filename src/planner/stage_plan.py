"""Stage planner for Cluster-CI v3.

Resolves DVC stage DAG, evaluates resource requirements from meta.cluster and .cluster-ci,
and computes node staleness and execution priorities without requiring heavy data.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx
from dvc.repo import Repo
from dvc.stage import PipelineStage

try:
    from src.config.defaults import (
        DEFAULT_RESOURCES,
        parse_project_cluster_ci,
        validate_and_resolve_resources,
    )
except ImportError:
    from config.defaults import (
        DEFAULT_RESOURCES,
        parse_project_cluster_ci,
        validate_and_resolve_resources,
    )


def _norm_path(p: str) -> str:
    """Normalize file path to forward slashes for cross-platform consistency."""
    return os.path.normpath(p).replace("\\", "/")


def _find_matching_output(
    dep_path: str, all_stage_outs: Dict[str, Tuple[str, Any]]
) -> Optional[Tuple[str, str, Any]]:
    """Check if dep_path is produced by an upstream stage.
    
    Returns (upstream_stage_name, out_key, out_obj) or None.
    Handles exact matches and subpaths inside directory outputs.
    """
    norm_dep = _norm_path(dep_path)
    if norm_dep in all_stage_outs:
        stage_name, out_obj = all_stage_outs[norm_dep]
        return stage_name, norm_dep, out_obj

    # Check if a directory output contains this dep
    for out_path, (stage_name, out_obj) in all_stage_outs.items():
        if norm_dep.startswith(out_path + "/"):
            return stage_name, out_path, out_obj

    return None


def compute_stage_plan(repo_path: str = ".") -> Dict[str, Any]:
    """Compute the versioned stage execution plan for a repository.
    
    Args:
        repo_path: Path to the target Git/DVC repository.
        
    Returns:
        A dictionary containing version, defaults, and list of resolved nodes.
        
    Raises:
        FileNotFoundError: If dvc.yaml does not exist in repo_path.
        ValueError/TypeError: If dvc.yaml or meta.cluster is invalid.
    """
    repo_path = os.path.abspath(repo_path)
    dvc_yaml_path = os.path.join(repo_path, "dvc.yaml")
    if not os.path.isfile(dvc_yaml_path):
        raise FileNotFoundError(f"dvc.yaml not found in repository at '{repo_path}'")

    # Load project-level overrides from .cluster-ci
    project_overrides = parse_project_cluster_ci(repo_path)

    # Initialize DVC Repo
    try:
        repo = Repo(repo_path)
    except Exception as e:
        raise ValueError(f"Failed to load DVC repository at '{repo_path}': {e}") from e

    # Collect pipeline stages and DVC file stages
    pipeline_stages: List[PipelineStage] = []
    dvc_file_outs: Dict[str, str] = {}  # norm_path -> md5 hash from .dvc files

    for s in repo.index.stages:
        if isinstance(s, PipelineStage) and getattr(s, "name", None):
            pipeline_stages.append(s)
        else:
            # Stage from a .dvc file
            for out in getattr(s, "outs", []):
                h = getattr(out, "hash_info", None)
                if h and getattr(h, "value", None):
                    dvc_file_outs[_norm_path(out.def_path)] = h.value

    # Map pipeline outputs: norm_path -> (stage_name, out_obj)
    all_pipeline_outs: Dict[str, Tuple[str, Any]] = {}
    for s in pipeline_stages:
        for out in getattr(s, "outs", []):
            all_pipeline_outs[_norm_path(out.def_path)] = (s.name, out)

    # Map pipeline stages by name
    stages_by_name: Dict[str, PipelineStage] = {s.name: s for s in pipeline_stages}

    # Inspect DAG graph from repo.index.graph
    # In DVC graph: an edge u -> v means u depends on v (v is an upstream predecessor)
    dvc_graph = repo.index.graph

    upstream_deps: Dict[str, List[str]] = {}
    downstream_deps: Dict[str, List[str]] = {}

    for s in pipeline_stages:
        # Stages that s depends on (successors in DVC's reversed dependency graph)
        up = [
            succ.name
            for succ in dvc_graph.successors(s)
            if hasattr(succ, "name") and succ.name in stages_by_name
        ]
        upstream_deps[s.name] = sorted(up)

        # Stages that depend on s (predecessors in DVC's reversed dependency graph)
        down = [
            pred.name
            for pred in dvc_graph.predecessors(s)
            if hasattr(pred, "name") and pred.name in stages_by_name
        ]
        downstream_deps[s.name] = sorted(down)

    # Load dvc.lock if present
    lock_path = os.path.join(repo_path, "dvc.lock")
    lock_stages: Dict[str, Any] = {}
    if os.path.isfile(lock_path):
        import yaml

        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                lock_data = yaml.safe_load(f)
                if isinstance(lock_data, dict):
                    lock_stages = lock_data.get("stages", {}) or {}
        except Exception as e:
            raise ValueError(f"Failed to read dvc.lock at '{lock_path}': {e}") from e

    # Step 1: Intrinsic staleness evaluation for each stage (without upstream propagation)
    intrinsic_stale: Dict[str, Tuple[bool, Optional[str]]] = {}

    for s in pipeline_stages:
        name = s.name

        # Rule 3: frozen -> never stale
        if getattr(s, "frozen", False):
            intrinsic_stale[name] = (False, None)
            continue

        # Rule 3: always_changed -> always stale
        if getattr(s, "always_changed", False):
            intrinsic_stale[name] = (True, "always_changed")
            continue

        # Rule 3: absent from dvc.lock -> stale
        if name not in lock_stages:
            intrinsic_stale[name] = (True, "not_in_lock")
            continue

        lock_stage = lock_stages[name]

        # Rule 3: params comparison
        is_stale = False
        stale_reason = None

        if hasattr(s, "params") and s.params:
            lock_params = lock_stage.get("params", {})
            for p in s.params:
                if not p.fs.exists(p.fs_path):
                    is_stale = True
                    stale_reason = f"params_file_missing:{_norm_path(p.def_path)}"
                    break
                try:
                    curr_params = p.read_params()
                except Exception:
                    is_stale = True
                    stale_reason = f"params_read_error:{_norm_path(p.def_path)}"
                    break

                lock_p = lock_params.get(p.def_path)
                if lock_p != curr_params:
                    is_stale = True
                    stale_reason = f"params_changed:{_norm_path(p.def_path)}"
                    break

        if is_stale:
            intrinsic_stale[name] = (is_stale, stale_reason)
            continue

        # Rule 3: dependencies comparison
        lock_deps_map: Dict[str, Dict[str, Any]] = {}
        for d in lock_stage.get("deps", []):
            if isinstance(d, dict) and "path" in d:
                lock_deps_map[_norm_path(d["path"])] = d

        for dep in getattr(s, "deps", []):
            if getattr(dep, "hash_name", None) == "params":
                continue

            dep_norm = _norm_path(dep.def_path)
            if dep_norm not in lock_deps_map:
                is_stale = True
                stale_reason = f"dep_not_in_lock:{dep_norm}"
                break

            lock_entry = lock_deps_map[dep_norm]
            lock_hash = lock_entry.get("md5") or lock_entry.get(
                lock_entry.get("hash", "md5")
            )

            # Case A: Dep produced by an upstream stage
            upstream_match = _find_matching_output(dep.def_path, all_pipeline_outs)
            if upstream_match:
                up_stage_name, up_out_path, _ = upstream_match
                up_lock = lock_stages.get(up_stage_name, {})
                up_lock_outs = {
                    _norm_path(o.get("path", "")): o
                    for o in up_lock.get("outs", [])
                    if isinstance(o, dict)
                }
                if up_out_path not in up_lock_outs:
                    is_stale = True
                    stale_reason = f"upstream_out_not_in_lock:{up_out_path}"
                    break
                up_lock_entry = up_lock_outs[up_out_path]
                up_hash = up_lock_entry.get("md5") or up_lock_entry.get(
                    up_lock_entry.get("hash", "md5")
                )
                if lock_hash != up_hash:
                    is_stale = True
                    stale_reason = f"upstream_hash_mismatch:{dep_norm}"
                    break
                # Valid upstream dependency in lock, no local heavy data check needed
                continue

            # Case B: Dep tracked by a .dvc file
            if dep_norm in dvc_file_outs:
                dvc_expected_hash = dvc_file_outs[dep_norm]
                if lock_hash != dvc_expected_hash:
                    is_stale = True
                    stale_reason = f"dot_dvc_hash_mismatch:{dep_norm}"
                    break
                continue

            # Case C: Dep tracked by git (code / configuration file)
            if not dep.fs.exists(dep.fs_path):
                is_stale = True
                stale_reason = f"code_dep_missing:{dep_norm}"
                break

            try:
                computed_hash = dep.get_hash().value
            except Exception as e:
                is_stale = True
                stale_reason = f"hash_calc_error:{dep_norm}:{e}"
                break

            if computed_hash != lock_hash:
                is_stale = True
                stale_reason = f"code_dep_changed:{dep_norm}"
                break

        intrinsic_stale[name] = (is_stale, stale_reason)

    # Step 2: Propagate staleness through DAG (upstream stale -> downstream stale)
    # Build a forward DAG (u -> v where u is upstream and v depends on u)
    forward_dag = nx.DiGraph()
    for s_name in stages_by_name:
        forward_dag.add_node(s_name)
    for s_name, ups in upstream_deps.items():
        for u in ups:
            forward_dag.add_edge(u, s_name)

    final_stale: Dict[str, bool] = {}
    final_stale_reason: Dict[str, Optional[str]] = {}

    # Traverse in topological order
    for s_name in nx.topological_sort(forward_dag):
        stage_obj = stages_by_name[s_name]

        # Frozen stages are never stale regardless of upstreams
        if getattr(stage_obj, "frozen", False):
            final_stale[s_name] = False
            final_stale_reason[s_name] = None
            continue

        is_stale, reason = intrinsic_stale[s_name]
        if is_stale:
            final_stale[s_name] = True
            final_stale_reason[s_name] = reason
            continue

        # Check if any upstream predecessor is stale
        upstream_stale_found = False
        for u in upstream_deps[s_name]:
            if final_stale.get(u, False):
                final_stale[s_name] = True
                final_stale_reason[s_name] = f"upstream_stale:{u}"
                upstream_stale_found = True
                break

        if not upstream_stale_found:
            final_stale[s_name] = False
            final_stale_reason[s_name] = None

    # Step 3: Compute priority = length of longest downstream remaining path of stale nodes (+1)
    memo_prio: Dict[str, int] = {}

    def _get_priority(node: str) -> int:
        if node in memo_prio:
            return memo_prio[node]
        if not final_stale[node]:
            memo_prio[node] = 0
            return 0

        # Downstream nodes that are also stale
        downstream_stale = [
            v for v in downstream_deps.get(node, []) if final_stale.get(v, False)
        ]
        if not downstream_stale:
            p = 1
        else:
            p = 1 + max(_get_priority(v) for v in downstream_stale)

        memo_prio[node] = p
        return p

    for s_name in stages_by_name:
        _get_priority(s_name)

    # Step 4: Assemble node objects
    nodes: List[Dict[str, Any]] = []

    for s in pipeline_stages:
        name = s.name

        # Parse resources
        meta = getattr(s, "meta", None)
        meta_cluster = meta.get("cluster") if isinstance(meta, dict) else None
        resources = validate_and_resolve_resources(
            name, meta_cluster, project_overrides
        )

        # Dep paths (non-params)
        dep_paths: List[str] = [
            _norm_path(d.def_path)
            for d in getattr(s, "deps", [])
            if getattr(d, "hash_name", None) != "params"
        ]

        # Out paths
        out_paths: List[Dict[str, Any]] = []
        for out in getattr(s, "outs", []):
            kind = "out"
            if getattr(out, "is_metric", False):
                kind = "metric"
            elif getattr(out, "is_plot", False):
                kind = "plot"

            out_paths.append(
                {
                    "path": _norm_path(out.def_path),
                    "cache": bool(getattr(out, "use_cache", True)),
                    "kind": kind,
                }
            )

        nodes.append(
            {
                "name": name,
                "deps": upstream_deps.get(name, []),
                "stale": final_stale[name],
                "stale_reason": final_stale_reason[name],
                "priority": memo_prio[name],
                "resources": resources,
                "dep_paths": dep_paths,
                "out_paths": out_paths,
            }
        )

    # Sort nodes topologically (or by stage order)
    # Using topological sort from forward_dag for natural execution order
    topo_order = {name: idx for idx, name in enumerate(nx.topological_sort(forward_dag))}
    nodes.sort(key=lambda n: topo_order.get(n["name"], 0))

    return {
        "version": 1,
        "defaults": DEFAULT_RESOURCES,
        "nodes": nodes,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Cluster-CI v3 stage planner: resolve DVC DAG and compute stale nodes."
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to repository with dvc.yaml (default: current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output plan in JSON format",
    )
    args = parser.parse_args()

    try:
        plan = compute_stage_plan(args.repo)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Always output formatted JSON if --json is passed or by default
    print(json.dumps(plan, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
