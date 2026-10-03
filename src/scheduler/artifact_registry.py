"""
Artifact Registry for Cluster-CI v3.

Tracks DVC artifacts / CAS objects produced by job nodes across cluster workers.
Provides:
  - Idempotent schema management (node_artifacts table with path & parent_dir_hash)
  - Recording node outputs from dvc.lock (hash, size, directory flag, worker location, sub-paths)
  - Data affinity calculation (total bytes of dependencies already present on a worker)
  - Multi-source routing (mapping dependency hashes to available online worker URLs)
  - Recursive directory manifest (.dir) inspection and parent-child dependency resolution
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Idempotently creates the node_artifacts table and associated indexes.
    
    Schema:
      node_artifacts(job_id, node_name, md5, is_dir, size_bytes, worker_id, created_at, path, parent_dir_hash)
    """
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS node_artifacts (
            job_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            md5 TEXT NOT NULL,
            is_dir INTEGER DEFAULT 0,
            size_bytes INTEGER DEFAULT 0,
            worker_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            path TEXT DEFAULT NULL,
            parent_dir_hash TEXT DEFAULT NULL,
            PRIMARY KEY (job_id, node_name, md5, worker_id)
        )
    """)
    # Additive migrations
    try:
        cursor.execute("ALTER TABLE node_artifacts ADD COLUMN path TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE node_artifacts ADD COLUMN parent_dir_hash TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_node_artifacts_md5 ON node_artifacts(md5)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_node_artifacts_worker ON node_artifacts(worker_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_node_artifacts_job ON node_artifacts(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_node_artifacts_parent ON node_artifacts(parent_dir_hash)")
    conn.commit()


def normalize_hash(h: Any) -> str:
    """Normalizes an MD5 hash string (lowercase, stripped)."""
    if h is None:
        return ""
    return str(h).strip().lower()


def record_node_outputs(
    conn: sqlite3.Connection,
    job_id: str,
    node: str,
    worker_id: str,
    outputs: Sequence[dict[str, Any] | str] | Mapping[str, Any] | None,
) -> int:
    """
    Records output artifacts produced by a node on a specific worker.
    
    Can accept:
      - list of dicts: [{"md5": "...", "size_bytes": 1024, "is_dir": False, "path": "...", "parent_dir_hash": "..."}, ...]
        or [{"hash": "...", "size": 1024}, ...] (as found in dvc.lock `outs`)
      - dict: {md5: size} or {md5: {"size_bytes": ..., "is_dir": ...}}
      - list of str: ["hash1", "hash2.dir", ...]
      
    Returns the number of artifacts recorded.
    """
    if not outputs:
        return 0

    ensure_schema(conn)

    parsed_rows: list[tuple[str, str, str, int, int, str, str | None, str | None]] = []

    if isinstance(outputs, Mapping):
        # Format {md5: size} or {md5: {"size_bytes": ..., "is_dir": ...}}
        for key, val in outputs.items():
            md5 = normalize_hash(key)
            if not md5:
                continue
            is_dir = 1 if md5.endswith(".dir") else 0
            size_bytes = 0
            path_val = None
            parent_hash = None
            if isinstance(val, (int, float)):
                size_bytes = int(val)
            elif isinstance(val, Mapping):
                size_bytes = int(val.get("size_bytes", val.get("size", 0)) or 0)
                if "is_dir" in val:
                    is_dir = 1 if val["is_dir"] else 0
                path_val = val.get("path")
                parent_hash = normalize_hash(val.get("parent_dir_hash")) or None
            parsed_rows.append((job_id, node, md5, is_dir, max(0, size_bytes), worker_id, path_val, parent_hash))

    elif isinstance(outputs, (list, tuple, set)):
        for item in outputs:
            if isinstance(item, str):
                md5 = normalize_hash(item)
                if not md5:
                    continue
                is_dir = 1 if md5.endswith(".dir") else 0
                parsed_rows.append((job_id, node, md5, is_dir, 0, worker_id, None, None))
            elif isinstance(item, Mapping):
                raw_hash = item.get("md5") or item.get("hash")
                md5 = normalize_hash(raw_hash)
                if not md5:
                    continue
                is_dir = 1 if (item.get("is_dir") or md5.endswith(".dir")) else 0
                size_bytes = int(item.get("size_bytes", item.get("size", 0)) or 0)
                path_val = item.get("path")
                parent_hash = normalize_hash(item.get("parent_dir_hash")) or None
                parsed_rows.append((job_id, node, md5, is_dir, max(0, size_bytes), worker_id, path_val, parent_hash))

    if not parsed_rows:
        return 0

    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT OR REPLACE INTO node_artifacts (
            job_id, node_name, md5, is_dir, size_bytes, worker_id, created_at, path, parent_dir_hash
        ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
        """,
        parsed_rows,
    )
    conn.commit()
    return len(parsed_rows)


def affinity_bytes(
    conn: sqlite3.Connection,
    dep_hashes: Iterable[str] | None,
    worker_id: str | None,
) -> int:
    """
    Computes the total volume (in bytes) of required dependency hashes already
    present in the CAS on `worker_id`. Includes directory manifest sizes (.dir)
    and resolves subfile hashes against recorded artifacts.
    
    If the same hash was recorded for multiple jobs on this worker, its size is
    counted exactly once.
    """
    if not dep_hashes or not worker_id:
        return 0

    ensure_schema(conn)

    clean_hashes = list({normalize_hash(h) for h in dep_hashes if normalize_hash(h)})
    if not clean_hashes:
        return 0

    cursor = conn.cursor()
    total_bytes = 0

    batch_size = 500
    for i in range(0, len(clean_hashes), batch_size):
        batch = clean_hashes[i : i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        # Matches either direct artifact md5 OR where worker holds the parent .dir
        query = f"""
            SELECT COALESCE(SUM(size_bytes), 0)
            FROM (
                SELECT md5, MAX(size_bytes) as size_bytes
                FROM node_artifacts
                WHERE worker_id = ? AND (md5 IN ({placeholders}) OR parent_dir_hash IN ({placeholders}))
                GROUP BY md5
            )
        """
        cursor.execute(query, [worker_id, *batch, *batch])
        row = cursor.fetchone()
        if row and row[0]:
            total_bytes += int(row[0])

    return total_bytes


def _resolve_online_workers_map(
    conn: sqlite3.Connection,
    online_workers: Any,
) -> dict[str, str]:
    """
    Helper that normalizes various online_workers structures into a mapping:
      worker_id -> service_url (e.g. 'http://worker-1:6000')
    """
    result: dict[str, str] = {}

    if isinstance(online_workers, Mapping):
        for w_id, val in online_workers.items():
            if isinstance(val, str):
                result[w_id] = val
            elif isinstance(val, Mapping):
                result[w_id] = str(val.get("service_url") or val.get("url") or f"http://{w_id}:6000")
            else:
                result[w_id] = f"http://{w_id}:6000"
        return result

    if isinstance(online_workers, (list, tuple, set)):
        worker_ids_to_lookup: list[str] = []
        for item in online_workers:
            if isinstance(item, str):
                worker_ids_to_lookup.append(item)
            elif isinstance(item, Mapping):
                w_id = item.get("worker_id")
                url = item.get("service_url") or item.get("url")
                if w_id:
                    result[w_id] = url or f"http://{w_id}:6000"

        if worker_ids_to_lookup:
            cursor = conn.cursor()
            try:
                placeholders = ",".join("?" for _ in worker_ids_to_lookup)
                cursor.execute(
                    f"SELECT worker_id, service_url FROM workers WHERE worker_id IN ({placeholders})",
                    worker_ids_to_lookup,
                )
                for row in cursor.fetchall():
                    w_id, s_url = row[0], row[1]
                    result[w_id] = s_url or f"http://{w_id}:6000"
            except sqlite3.OperationalError:
                pass

            for w_id in worker_ids_to_lookup:
                if w_id not in result:
                    result[w_id] = f"http://{w_id}:6000"

    return result


def sources_for(
    conn: sqlite3.Connection,
    dep_hashes: Iterable[str] | None,
    online_workers: Any,
) -> dict[str, list[str]]:
    """
    Maps each dependency MD5 hash to the list of URLs of online workers currently
    holding that artifact (including resolving subfiles via parent .dir outputs).
    
    Args:
      conn: SQLite database connection
      dep_hashes: Collection of requested MD5 hashes (including .dir and subfiles)
      online_workers: Dict mapping worker_id -> service_url, or list of worker objects/IDs.
      
    Returns:
      dict mapping {md5: [url_worker_1, url_worker_2, ...]}
    """
    if not dep_hashes:
        return {}

    ensure_schema(conn)

    clean_hashes = list({normalize_hash(h) for h in dep_hashes if normalize_hash(h)})
    sources_map: dict[str, list[str]] = {h: [] for h in clean_hashes}
    if not clean_hashes:
        return sources_map

    worker_url_map = _resolve_online_workers_map(conn, online_workers)
    if not worker_url_map:
        return sources_map

    online_ids = list(worker_url_map.keys())
    cursor = conn.cursor()

    batch_size = 300
    for i in range(0, len(clean_hashes), batch_size):
        h_batch = clean_hashes[i : i + batch_size]
        h_placeholders = ",".join("?" for _ in h_batch)
        w_placeholders = ",".join("?" for _ in online_ids)

        # 1. Query artifacts matching directly by md5 OR where worker holds the parent .dir
        query = f"""
            SELECT md5, worker_id, parent_dir_hash, MAX(created_at) as latest_created
            FROM node_artifacts
            WHERE worker_id IN ({w_placeholders})
              AND (md5 IN ({h_placeholders}) OR parent_dir_hash IN ({h_placeholders}))
            GROUP BY md5, worker_id
            ORDER BY latest_created DESC
        """
        cursor.execute(query, [*online_ids, *h_batch, *h_batch])
        for row in cursor.fetchall():
            md5_val, w_id, parent_dir_hash = row[0], row[1], row[2]
            url = worker_url_map.get(w_id)
            if not url:
                continue

            # Direct match
            if md5_val in sources_map and url not in sources_map[md5_val]:
                sources_map[md5_val].append(url)

            # If this artifact's parent_dir_hash was requested, worker holding the file also holds parent
            if parent_dir_hash and parent_dir_hash in sources_map and url not in sources_map[parent_dir_hash]:
                sources_map[parent_dir_hash].append(url)

        # 2. Also check if requested hash is a subfile whose parent .dir is held by an online worker
        query_parent = f"""
            SELECT sub.md5, parent.worker_id, MAX(parent.created_at) as latest_created
            FROM node_artifacts sub
            JOIN node_artifacts parent ON sub.parent_dir_hash = parent.md5
            WHERE sub.md5 IN ({h_placeholders})
              AND parent.worker_id IN ({w_placeholders})
            GROUP BY sub.md5, parent.worker_id
            ORDER BY latest_created DESC
        """
        try:
            cursor.execute(query_parent, [*h_batch, *online_ids])
            for row in cursor.fetchall():
                sub_md5, w_id = row[0], row[1]
                url = worker_url_map.get(w_id)
                if url and sub_md5 in sources_map and url not in sources_map[sub_md5]:
                    sources_map[sub_md5].append(url)
        except sqlite3.OperationalError:
            pass

    return sources_map


def extract_node_outputs_from_dvc_lock(
    dvc_lock_data: str | Mapping[str, Any],
    node_name: str,
    repo_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """
    Utility for W2/W3 to extract output definitions directly from dvc.lock
    for a given stage/node.
    
    If an output is a directory (.dir), inspects the local manifest file
    (if present in repo_dir or relative to dvc.lock) and also yields all
    nested files with their parent_dir_hash.
    
    Returns list of dicts:
      [{"path": "...", "md5": "...", "size_bytes": 1234, "is_dir": bool, "parent_dir_hash": ...}]
    """
    lock_file_path = None
    if isinstance(dvc_lock_data, str):
        try:
            import yaml  # type: ignore
        except ImportError:
            import json as yaml  # type: ignore

        if "\n" not in dvc_lock_data and (dvc_lock_data.endswith(".lock") or dvc_lock_data.endswith(".yaml")):
            if os.path.isfile(dvc_lock_data):
                lock_file_path = dvc_lock_data
                with open(dvc_lock_data, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            else:
                data = yaml.safe_load(dvc_lock_data)
        else:
            data = yaml.safe_load(dvc_lock_data)
    else:
        data = dvc_lock_data

    if not isinstance(data, Mapping):
        return []

    stages = data.get("stages", {})
    stage = stages.get(node_name, {})
    outs = stage.get("outs", [])

    # Determine base directory to find .dvc cache for manifest parsing
    base_repo: Path | None = None
    if repo_dir:
        base_repo = Path(repo_dir)
    elif lock_file_path:
        base_repo = Path(lock_file_path).parent

    results = []
    for out in outs:
        if not isinstance(out, Mapping):
            continue
        raw_hash = out.get("md5") or out.get("hash")
        if not raw_hash:
            continue
        md5 = normalize_hash(raw_hash)
        is_dir = bool(out.get("is_dir") or md5.endswith(".dir"))
        size_bytes = int(out.get("size_bytes", out.get("size", 0)) or 0)
        out_path = str(out.get("path", "")).replace("\\", "/")

        results.append({
            "path": out_path,
            "md5": md5,
            "size_bytes": max(0, size_bytes),
            "is_dir": is_dir,
            "parent_dir_hash": None,
        })

        # If it's a directory, parse manifest to index all nested files
        if is_dir and base_repo:
            manifest_file = base_repo / ".dvc" / "cache" / "files" / "md5" / md5[:2] / md5[2:]
            if manifest_file.is_file():
                try:
                    with open(manifest_file, "r", encoding="utf-8") as mf:
                        manifest_data = json.load(mf)
                    if isinstance(manifest_data, list):
                        for entry in manifest_data:
                            sub_md5 = normalize_hash(entry.get("md5"))
                            rel = str(entry.get("relpath", "")).replace("\\", "/")
                            sub_size = int(entry.get("size", 0) or 0)
                            if sub_md5:
                                results.append({
                                    "path": f"{out_path}/{rel}",
                                    "md5": sub_md5,
                                    "size_bytes": max(0, sub_size),
                                    "is_dir": False,
                                    "parent_dir_hash": md5,
                                })
                except Exception as e:
                    logger.debug("Failed to inspect .dir manifest %s: %s", manifest_file, e)

    return results


def get_dag_stage_outputs(
    dvc_lock_data: str | Mapping[str, Any] | None = None,
    dvc_yaml_data: str | Mapping[str, Any] | None = None,
    repo_dir: str | Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[tuple[Any, str]]]:
    """
    Collects all stage outputs across the entire DAG from dvc.lock and dvc.yaml.
    Handles foreach stages (expanded in dvc.lock as stage@item, and template patterns in dvc.yaml).
    
    Returns:
      (exact_outputs, directory_outputs, pattern_outputs)
      where:
        exact_outputs: {norm_path: {"stage": stage_name, "md5": md5_or_none, "is_dir": bool}}
        directory_outputs: {norm_dir_path: {"stage": stage_name, "md5": md5_or_none, "is_dir": True}}
        pattern_outputs: [(compiled_regex, stage_name)]
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        import json as yaml  # type: ignore

    exact_outputs: dict[str, dict[str, Any]] = {}
    directory_outputs: dict[str, dict[str, Any]] = {}
    pattern_outputs: list[tuple[Any, str]] = []

    def _load_yaml(src: str | Mapping[str, Any] | None) -> dict[str, Any]:
        if not src:
            return {}
        if isinstance(src, Mapping):
            return dict(src)
        if isinstance(src, str):
            if "\n" not in src and os.path.isfile(src):
                try:
                    with open(src, "r", encoding="utf-8") as f:
                        return yaml.safe_load(f) or {}
                except Exception:
                    return {}
            else:
                try:
                    return yaml.safe_load(src) or {}
                except Exception:
                    return {}
        return {}

    lock_dict: dict[str, Any] = {}
    yaml_dict: dict[str, Any] = {}

    if repo_dir:
        rpath = Path(repo_dir)
        lock_file = rpath / "dvc.lock"
        if lock_file.is_file():
            lock_dict = _load_yaml(str(lock_file))
        yaml_file = rpath / "dvc.yaml"
        if yaml_file.is_file():
            yaml_dict = _load_yaml(str(yaml_file))

    if dvc_lock_data:
        ld = _load_yaml(dvc_lock_data)
        if ld:
            lock_dict = ld

    if dvc_yaml_data:
        yd = _load_yaml(dvc_yaml_data)
        if yd:
            yaml_dict = yd

    # 1. Parse dvc.lock stages
    lock_stages = lock_dict.get("stages", {}) if isinstance(lock_dict, Mapping) else {}
    if isinstance(lock_stages, Mapping):
        for st_name, st_val in lock_stages.items():
            if not isinstance(st_val, Mapping):
                continue
            for out in st_val.get("outs", []):
                if not isinstance(out, Mapping):
                    continue
                raw_path = out.get("path")
                if not raw_path:
                    continue
                norm_p = os.path.normpath(str(raw_path)).replace("\\", "/").rstrip("/")
                if not norm_p or norm_p == ".":
                    continue
                raw_hash = out.get("md5") or out.get("hash")
                h = normalize_hash(raw_hash) if raw_hash else None
                is_dir = bool(out.get("is_dir") or (h and h.endswith(".dir")))
                info = {"stage": str(st_name), "md5": h, "is_dir": is_dir}
                exact_outputs[norm_p] = info
                if is_dir:
                    directory_outputs[norm_p] = info

    def _extract_outs(outs_obj: Any) -> list[str]:
        paths = []
        if isinstance(outs_obj, list):
            for item in outs_obj:
                if isinstance(item, str):
                    paths.append(item)
                elif isinstance(item, Mapping):
                    for k, v in item.items():
                        if k == "path" and isinstance(v, str):
                            paths.append(v)
                        elif isinstance(k, str) and not isinstance(v, Mapping):
                            paths.append(k)
                        elif isinstance(item.get("path"), str):
                            paths.append(item["path"])
        elif isinstance(outs_obj, str):
            paths.append(outs_obj)
        elif isinstance(outs_obj, Mapping):
            for k in outs_obj.keys():
                if isinstance(k, str):
                    paths.append(k)
        return paths

    # 2. Parse dvc.yaml stages
    yaml_stages = yaml_dict.get("stages", {}) if isinstance(yaml_dict, Mapping) else {}
    if isinstance(yaml_stages, Mapping):
        for st_name, st_def in yaml_stages.items():
            if not isinstance(st_def, Mapping):
                continue

            foreach_items = st_def.get("foreach")
            do_block = st_def.get("do", {})
            if foreach_items is not None and isinstance(do_block, Mapping):
                do_outs = _extract_outs(do_block.get("outs", []))
                do_outs.extend(_extract_outs(do_block.get("metrics", [])))
                do_outs.extend(_extract_outs(do_block.get("plots", [])))

                items_list: list[str] = []
                if isinstance(foreach_items, list):
                    items_list = [str(x) for x in foreach_items]
                elif isinstance(foreach_items, Mapping):
                    items_list = [str(k) for k in foreach_items.keys()]

                for item_val in items_list:
                    sub_stage = f"{st_name}@{item_val}"
                    for raw_p in do_outs:
                        resolved = raw_p.replace("${item}", item_val).replace("$item", item_val)
                        norm_p = os.path.normpath(resolved).replace("\\", "/").rstrip("/")
                        if not norm_p or norm_p == ".":
                            continue
                        is_dir = raw_p.endswith("/") or raw_p.endswith("\\")
                        if norm_p not in exact_outputs:
                            exact_outputs[norm_p] = {"stage": sub_stage, "md5": None, "is_dir": is_dir}
                        if is_dir and norm_p not in directory_outputs:
                            directory_outputs[norm_p] = {"stage": sub_stage, "md5": None, "is_dir": True}

                for raw_p in do_outs:
                    norm_p = os.path.normpath(raw_p).replace("\\", "/").rstrip("/")
                    if "${" in norm_p or "$" in norm_p:
                        pat_str = re.escape(norm_p)
                        pat_str = re.sub(r'\\\$\\\{[^}]+\\\}', '.*', pat_str)
                        pat_str = re.sub(r'\\\$[a-zA-Z0-9_]+', '.*', pat_str)
                        try:
                            pattern_outputs.append((re.compile(f"^{pat_str}(?:/.*)?$"), str(st_name)))
                        except Exception:
                            pass
            else:
                st_outs = _extract_outs(st_def.get("outs", []))
                st_outs.extend(_extract_outs(st_def.get("metrics", [])))
                st_outs.extend(_extract_outs(st_def.get("plots", [])))
                for raw_p in st_outs:
                    norm_p = os.path.normpath(raw_p).replace("\\", "/").rstrip("/")
                    if not norm_p or norm_p == ".":
                        continue
                    is_dir = raw_p.endswith("/") or raw_p.endswith("\\")
                    if norm_p not in exact_outputs:
                        exact_outputs[norm_p] = {"stage": str(st_name), "md5": None, "is_dir": is_dir}
                    if is_dir and norm_p not in directory_outputs:
                        directory_outputs[norm_p] = {"stage": str(st_name), "md5": None, "is_dir": True}

    return exact_outputs, directory_outputs, pattern_outputs


def is_dag_stage_output(
    dep_path: str,
    exact_outputs: Mapping[str, Any],
    directory_outputs: Mapping[str, Any],
    pattern_outputs: Sequence[Any],
) -> tuple[bool, dict[str, Any] | None]:
    """
    Checks if dep_path is an output of any stage in the DAG.
    Returns (True, info_dict) or (False, None).
    """
    norm_dep = os.path.normpath(dep_path).replace("\\", "/").rstrip("/")
    if norm_dep in exact_outputs:
        return True, dict(exact_outputs[norm_dep])
    for dir_path, dir_info in directory_outputs.items():
        if norm_dep.startswith(f"{dir_path}/"):
            res = dict(dir_info)
            res["parent_dir"] = dir_path
            res["parent_dir_hash"] = dir_info.get("md5")
            return True, res
    for pat, st_name in pattern_outputs:
        if pat.match(norm_dep):
            return True, {"stage": st_name, "md5": None, "is_dir": False}
    return False, None


def extract_node_deps_from_dvc_lock(
    dvc_lock_data: str | Mapping[str, Any],
    node_name: str,
    stage_outs_only: bool = False,
    repo_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """
    Utility for W2/W3 to extract dependency definitions directly from dvc.lock
    for a given stage/node.
    
    Automatically resolves sub-paths belonging to upstream directory outputs (.dir)
    and attaches parent_dir_hash so that the parent .dir manifest can be retrieved.
    
    When stage_outs_only=True, filters out git-tracked files (scripts, configs)
    that are not the output of any stage in the DAG.
    
    Returns list of dicts:
      [{"path": "...", "md5": "...", "size_bytes": 1234, "is_dir": bool, "parent_dir_hash": ..., "is_stage_output": bool}]
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        import json as yaml  # type: ignore

    lock_file_path = None
    if isinstance(dvc_lock_data, str):
        if "\n" not in dvc_lock_data and (dvc_lock_data.endswith(".lock") or dvc_lock_data.endswith(".yaml")):
            if os.path.isfile(dvc_lock_data):
                lock_file_path = dvc_lock_data
                with open(dvc_lock_data, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            else:
                data = yaml.safe_load(dvc_lock_data)
        else:
            data = yaml.safe_load(dvc_lock_data)
    else:
        data = dvc_lock_data

    if not isinstance(data, Mapping):
        return []

    effective_repo_dir = repo_dir
    if not effective_repo_dir and lock_file_path:
        effective_repo_dir = os.path.dirname(lock_file_path)

    exact_outs, dir_outs, pat_outs = get_dag_stage_outputs(
        dvc_lock_data=data,
        repo_dir=effective_repo_dir,
    )

    stages = data.get("stages", {})
    stage = stages.get(node_name, {})
    deps = stage.get("deps", [])

    results = []
    parent_dirs_to_add: dict[str, str] = {}  # {parent_md5: dir_path}

    for dep in deps:
        if not isinstance(dep, Mapping):
            continue
        raw_hash = dep.get("md5") or dep.get("hash")
        if not raw_hash:
            continue
        md5 = normalize_hash(raw_hash)
        is_dir = bool(dep.get("is_dir") or md5.endswith(".dir"))
        size_bytes = int(dep.get("size_bytes", dep.get("size", 0)) or 0)
        dep_path = str(dep.get("path", "")).replace("\\", "/")

        is_stage_out, out_info = is_dag_stage_output(dep_path, exact_outs, dir_outs, pat_outs)
        if stage_outs_only and not is_stage_out:
            continue

        parent_dir_hash = (out_info or {}).get("parent_dir_hash")
        if not parent_dir_hash:
            for dir_path, dir_md5 in dir_outs.items():
                if dep_path.startswith(f"{dir_path}/"):
                    parent_dir_hash = dir_md5.get("md5") if isinstance(dir_md5, Mapping) else dir_md5
                    parent_dirs_to_add[str(parent_dir_hash)] = dir_path
                    break
        elif parent_dir_hash:
            p_dir = (out_info or {}).get("parent_dir") or ""
            parent_dirs_to_add[str(parent_dir_hash)] = p_dir

        results.append({
            "path": dep_path,
            "md5": md5,
            "size_bytes": max(0, size_bytes),
            "is_dir": is_dir,
            "parent_dir_hash": parent_dir_hash,
            "is_stage_output": is_stage_out,
        })

    # Ensure parent .dir manifests are also declared so DVC checkout can unpack subfiles
    existing_hashes = {r["md5"] for r in results}
    for p_md5, p_path in parent_dirs_to_add.items():
        if p_md5 and p_md5 not in existing_hashes:
            results.append({
                "path": p_path,
                "md5": p_md5,
                "size_bytes": 0,
                "is_dir": True,
                "parent_dir_hash": None,
                "is_stage_output": True,
            })
            existing_hashes.add(p_md5)

    return results
