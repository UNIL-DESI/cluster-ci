"""Stage planner for Cluster-CI v3.

Resolves DVC stage DAG, evaluates resource requirements from meta.cluster and .cluster-ci,
and computes node staleness and execution priorities without requiring heavy data.
"""

import argparse
import copy
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple, Union

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


def compute_stage_plan(
    repo_path: str = ".",
    target_stages: Optional[Union[List[str], str]] = None,
) -> Dict[str, Any]:
    """Compute the versioned stage execution plan for a repository.
    
    Args:
        repo_path: Path to the target Git/DVC repository.
        target_stages: Optional target stage name(s) to restrict the execution plan to.
            When provided, only the target stages and their transitive upstream closure
            (dependencies) are scheduled. Ancestors already up-to-date remain skipped.
            Supports exact names ('prep'), foreach instances ('train@item1') and
            base foreach names ('train' matching all 'train@*').
        
    Returns:
        A dictionary containing version, defaults, and list of resolved nodes.
        
    Raises:
        FileNotFoundError: If dvc.yaml does not exist in repo_path.
        ValueError/TypeError: If dvc.yaml, meta.cluster, or target_stages is invalid.
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

    # Build initial forward DAG (u -> v where u is upstream and v depends on u)
    forward_dag = nx.DiGraph()
    for s_name in stages_by_name:
        forward_dag.add_node(s_name)
    for s_name, ups in upstream_deps.items():
        for u in ups:
            forward_dag.add_edge(u, s_name)

    # Resolve target stages (Bug 11 - filter plan to target stages + upstream closure)
    raw_targets = target_stages
    if raw_targets is None:
        raw_targets = project_overrides.get("stages")

    cleaned_targets: List[str] = []
    if raw_targets:
        if isinstance(raw_targets, str):
            raw_items = [raw_targets]
        else:
            raw_items = list(raw_targets)
        for item in raw_items:
            if item:
                for sub in re.split(r'[\s,]+', str(item)):
                    sub_clean = sub.strip()
                    if sub_clean:
                        cleaned_targets.append(sub_clean)

    if cleaned_targets:
        all_valid_names = set(stages_by_name.keys())
        resolved_targets: Set[str] = set()

        for t in cleaned_targets:
            matched = False
            if t in all_valid_names:
                resolved_targets.add(t)
                matched = True
            else:
                # Check foreach base name (stage@item matches t)
                prefix = f"{t}@"
                foreach_matches = [s for s in all_valid_names if s.startswith(prefix)]
                if foreach_matches:
                    resolved_targets.update(foreach_matches)
                    matched = True

            if not matched:
                sorted_valid = sorted(all_valid_names)
                raise ValueError(
                    f"Nom de stage inconnu dans STAGES : '{t}'. "
                    f"Cause : aucun stage ne correspond à ce nom ou à ce préfixe foreach dans dvc.yaml. "
                    f"Stages valides disponibles : {sorted_valid}."
                )

        # Transitive upstream closure: targets + all upstream ancestors
        target_closure: Set[str] = set(resolved_targets)
        for t in resolved_targets:
            target_closure.update(nx.ancestors(forward_dag, t))

        # Restrict graph and stage structures to upstream closure
        forward_dag = forward_dag.subgraph(target_closure).copy()
        pipeline_stages = [s for s in pipeline_stages if s.name in target_closure]
        stages_by_name = {s.name: s for s in pipeline_stages}
        upstream_deps = {
            s_name: [u for u in ups if u in target_closure]
            for s_name, ups in upstream_deps.items()
            if s_name in target_closure
        }
        downstream_deps = {
            s_name: [d for d in downs if d in target_closure]
            for s_name, downs in downstream_deps.items()
            if s_name in target_closure
        }

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
                # Check if this dependency is a subpath of an upstream directory output
                # (e.g., dep '.dvc-viewer/hashes/xxx.hash' while upstream outputs '.dvc-viewer/hashes' with a .dir hash)
                is_subpath = (dep_norm != up_out_path) or (bool(up_hash) and str(up_hash).endswith(".dir"))

                if is_subpath:
                    # Semantic of DVC 3.67.1 (dvc status): Never compare a file md5 to a .dir md5!
                    # 1. If the subpath file exists on disk/workspace, evaluate its hash using DVC hasher:
                    if dep.fs.exists(dep.fs_path):
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
                        # File on disk matches the dvc.lock entry: dependency is up-to-date
                        continue
                    else:
                        # 2. If the file is not on disk (heavy data not pulled), check if .dir cache exists locally
                        dir_cache_matched = False
                        if up_hash and hasattr(repo, "odb") and hasattr(repo.odb, "local"):
                            try:
                                from dvc_data.hashfile.tree import Tree
                                tree = Tree.load(repo.odb.local, dep.hash_info.__class__(name="md5", value=up_hash))
                                rel_in_dir = os.path.relpath(dep_norm, up_out_path).replace("\\", "/")
                                obj = tree.get(rel_in_dir)
                                if obj and hasattr(obj, "hash_info") and obj.hash_info:
                                    if lock_hash != obj.hash_info.value:
                                        is_stale = True
                                        stale_reason = f"upstream_hash_mismatch:{dep_norm}"
                                        break
                                    dir_cache_matched = True
                            except Exception:
                                pass
                        # If verified via dir cache or heavy data absent (abstracted without heavy data),
                        # the lock entry is valid. Staleness will follow upstream stage status.
                        continue
                else:
                    # Exact output match (both are files or both are identical directory outputs):
                    if dep.fs.exists(dep.fs_path):
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
        # Propagation rule for always_changed:
        # DVC does NOT invalidate downstream stages simply because an upstream stage is
        # marked 'always_changed: true'. Downstream stages only become stale if the outputs
        # of the always_changed stage actually change after execution.
        # Therefore, an upstream node stale solely due to 'always_changed' does NOT propagate
        # staleness to its descendants. Descendants are evaluated on their own dependencies.
        # Once the upstream node finishes, replan() re-evaluates downstream staleness against
        # the freshly committed/synchronized outputs.
        upstream_stale_found = False
        for u in upstream_deps[s_name]:
            if final_stale.get(u, False) and final_stale_reason.get(u) != "always_changed":
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
                "scheduling_priority": resources.get("priority", "normal"),
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
        "version": "3.0",
        "defaults": DEFAULT_RESOURCES,
        "nodes": nodes,
    }


def filter_plan_to_stages(
    plan: Dict[str, Any],
    target_stages: Union[str, List[str]],
    mark_skipped: bool = True,
) -> Dict[str, Any]:
    """Filter an execution plan according to target stages and their upstream closure.

    Args:
        plan: The plan dictionary containing 'nodes'.
        target_stages: String or list of target stage names (e.g. 'branch_b_step2').
        mark_skipped: If True, keep non-selected nodes in the plan with stale=False,
            stale_reason='skipped_by_stages_filter', and priority=0. If False, prune
            non-selected nodes entirely from plan['nodes'].

    Returns:
        The filtered plan dictionary.
    """
    if not target_stages or not plan or "nodes" not in plan:
        return plan

    cleaned_targets: List[str] = []
    if isinstance(target_stages, str):
        raw_items = [target_stages]
    else:
        raw_items = list(target_stages)
    for item in raw_items:
        if item:
            for sub in re.split(r'[\s,]+', str(item)):
                sub_clean = sub.strip()
                if sub_clean:
                    cleaned_targets.append(sub_clean)

    if not cleaned_targets:
        return plan

    nodes = plan.get("nodes", [])
    all_valid_names = {n["name"] for n in nodes}
    resolved_targets: Set[str] = set()

    for t in cleaned_targets:
        matched = False
        if t in all_valid_names:
            resolved_targets.add(t)
            matched = True
        else:
            prefix = f"{t}@"
            foreach_matches = [s for s in all_valid_names if s.startswith(prefix)]
            if foreach_matches:
                resolved_targets.update(foreach_matches)
                matched = True

        if not matched:
            raise ValueError(
                f"Nom de stage inconnu dans STAGES : '{t}'. "
                f"Cause : aucun stage ne correspond à ce nom ou à ce préfixe foreach dans le plan. "
                f"Stages valides disponibles : {sorted(all_valid_names)}."
            )

    forward_dag = nx.DiGraph()
    for n in nodes:
        name = n["name"]
        forward_dag.add_node(name)
        for u in n.get("deps", []):
            forward_dag.add_edge(u, name)

    target_closure: Set[str] = set(resolved_targets)
    for t in resolved_targets:
        if t in forward_dag:
            target_closure.update(nx.ancestors(forward_dag, t))

    if mark_skipped:
        filtered_nodes = []
        for n in nodes:
            name = n["name"]
            n_copy = copy.deepcopy(n)
            if name in target_closure:
                filtered_nodes.append(n_copy)
            else:
                n_copy["stale"] = False
                n_copy["stale_reason"] = "skipped_by_stages_filter"
                n_copy["priority"] = 0
                filtered_nodes.append(n_copy)
        plan["nodes"] = filtered_nodes
    else:
        plan["nodes"] = [n for n in nodes if n["name"] in target_closure]

    return plan


def replan(
    repo_path: str = ".",
    done_node: Optional[str] = None,
    target_stages: Optional[Union[List[str], str]] = None,
) -> Dict[str, Any]:
    """Re-evaluate the stage execution plan after a node has finished execution.

    In Cluster-CI v3, when an upstream node completes on a worker (e.g., an
    always_changed node or a stage whose code changed), its updated outputs are
    committed and synchronized into git and dvc.lock.

    Calling `replan` recomputes the full DAG staleness on the updated repository.
    Because `always_changed` does not blindly contaminate descendants, any downstream
    node whose input dependencies match the new outputs will be evaluated as up-to-date
    (stale=False), allowing the scheduler to safely prune or skip unnecessary runs.

    Execution time on large real pipelines (such as llm-as-recommender with 83 nodes)
    is ~1.2 s (< 10 s threshold), making full DAG replanning at node completion
    completely acceptable.

    Args:
        repo_path: Path to the target Git/DVC repository with updated state.
        done_node: Optional name of the completed stage node (for logging/traceability).
        target_stages: Optional target stage name(s) to restrict the execution plan to.

    Returns:
        The updated versioned stage execution plan dictionary.
    """
    return compute_stage_plan(repo_path=repo_path, target_stages=target_stages)


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
    parser.add_argument(
        "--done-node",
        default=None,
        help="Optional name of completed stage node when replanning",
    )
    parser.add_argument(
        "--stages",
        nargs="*",
        default=None,
        help="Target stages to restrict the execution plan to (closure includes target and upstreams)",
    )
    args = parser.parse_args()

    stages_arg: Optional[List[str]] = None
    if args.stages:
        stages_arg = []
        for item in args.stages:
            stages_arg.extend([s.strip() for s in re.split(r'[\s,]+', item) if s.strip()])

    try:
        if args.done_node:
            plan = replan(args.repo, done_node=args.done_node, target_stages=stages_arg)
        else:
            plan = compute_stage_plan(args.repo, target_stages=stages_arg)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Always output formatted JSON if --json is passed or by default
    print(json.dumps(plan, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
