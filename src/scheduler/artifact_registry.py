"""
Artifact Registry for Cluster-CI v3.

Tracks DVC artifacts / CAS objects produced by job nodes across cluster workers.
Provides:
  - Idempotent schema management (node_artifacts table)
  - Recording node outputs from dvc.lock (hash, size, directory flag, worker location)
  - Data affinity calculation (total bytes of dependencies already present on a worker)
  - Multi-source routing (mapping dependency hashes to available online worker URLs)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Idempotently creates the node_artifacts table and associated indexes.
    
    Schema:
      node_artifacts(job_id, node_name, md5, is_dir, size_bytes, worker_id, created_at)
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
            PRIMARY KEY (job_id, node_name, md5, worker_id)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_node_artifacts_md5 ON node_artifacts(md5)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_node_artifacts_worker ON node_artifacts(worker_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_node_artifacts_job ON node_artifacts(job_id)
    """)
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
      - list of dicts: [{"md5": "...", "size_bytes": 1024, "is_dir": False, ...}, ...]
        or [{"hash": "...", "size": 1024}, ...] (as found in dvc.lock `outs`)
      - dict: {md5: size} or {md5: {"size_bytes": ..., "is_dir": ...}}
      - list of str: ["hash1", "hash2.dir", ...]
      
    Returns the number of artifacts recorded.
    """
    if not outputs:
        return 0

    ensure_schema(conn)

    parsed_rows: list[tuple[str, str, str, int, int, str]] = []

    if isinstance(outputs, Mapping):
        # Format {md5: size} or {md5: {"size_bytes": ..., "is_dir": ...}}
        for key, val in outputs.items():
            md5 = normalize_hash(key)
            if not md5:
                continue
            is_dir = 1 if md5.endswith(".dir") else 0
            size_bytes = 0
            if isinstance(val, (int, float)):
                size_bytes = int(val)
            elif isinstance(val, Mapping):
                size_bytes = int(val.get("size_bytes", val.get("size", 0)) or 0)
                if "is_dir" in val:
                    is_dir = 1 if val["is_dir"] else 0
            parsed_rows.append((job_id, node, md5, is_dir, max(0, size_bytes), worker_id))

    elif isinstance(outputs, (list, tuple, set)):
        for item in outputs:
            if isinstance(item, str):
                md5 = normalize_hash(item)
                if not md5:
                    continue
                is_dir = 1 if md5.endswith(".dir") else 0
                parsed_rows.append((job_id, node, md5, is_dir, 0, worker_id))
            elif isinstance(item, Mapping):
                raw_hash = item.get("md5") or item.get("hash")
                md5 = normalize_hash(raw_hash)
                if not md5:
                    continue
                is_dir = 1 if (item.get("is_dir") or md5.endswith(".dir")) else 0
                size_bytes = int(item.get("size_bytes", item.get("size", 0)) or 0)
                parsed_rows.append((job_id, node, md5, is_dir, max(0, size_bytes), worker_id))

    if not parsed_rows:
        return 0

    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT OR REPLACE INTO node_artifacts (
            job_id, node_name, md5, is_dir, size_bytes, worker_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
    present in the CAS on `worker_id`. Includes directory manifest sizes (.dir).
    
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

    # Batch queries to avoid SQLite parameter limit
    batch_size = 500
    for i in range(0, len(clean_hashes), batch_size):
        batch = clean_hashes[i : i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        query = f"""
            SELECT COALESCE(SUM(size_bytes), 0)
            FROM (
                SELECT md5, MAX(size_bytes) as size_bytes
                FROM node_artifacts
                WHERE worker_id = ? AND md5 IN ({placeholders})
                GROUP BY md5
            )
        """
        cursor.execute(query, [worker_id, *batch])
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
            # Try to lookup service_url from workers table if available
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
                # workers table doesn't exist in standalone/test database
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
    holding that artifact.
    
    Args:
      conn: SQLite database connection
      dep_hashes: Collection of requested MD5 hashes (including .dir)
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

    # Query matching artifacts on online workers
    # Sort by created_at DESC so the most recent source comes first
    batch_size = 300
    for i in range(0, len(clean_hashes), batch_size):
        h_batch = clean_hashes[i : i + batch_size]
        h_placeholders = ",".join("?" for _ in h_batch)
        w_placeholders = ",".join("?" for _ in online_ids)

        query = f"""
            SELECT md5, worker_id, MAX(created_at) as latest_created
            FROM node_artifacts
            WHERE md5 IN ({h_placeholders}) AND worker_id IN ({w_placeholders})
            GROUP BY md5, worker_id
            ORDER BY latest_created DESC
        """
        cursor.execute(query, [*h_batch, *online_ids])
        for row in cursor.fetchall():
            md5_val, w_id = row[0], row[1]
            url = worker_url_map.get(w_id)
            if url and url not in sources_map[md5_val]:
                sources_map[md5_val].append(url)

    return sources_map


def extract_node_outputs_from_dvc_lock(
    dvc_lock_data: str | Mapping[str, Any],
    node_name: str,
) -> list[dict[str, Any]]:
    """
    Utility for W2/W3 to extract output definitions directly from dvc.lock
    (either YAML string, path, or parsed dictionary) for a given stage/node.
    
    Returns list of dicts:
      [{"path": "...", "md5": "...", "size_bytes": 1234, "is_dir": bool}]
    """
    if isinstance(dvc_lock_data, str):
        # Could be path or raw YAML content
        try:
            import yaml  # type: ignore
        except ImportError:
            import json as yaml  # type: ignore

        if "\n" not in dvc_lock_data and (dvc_lock_data.endswith(".lock") or dvc_lock_data.endswith(".yaml")):
            import os
            if os.path.isfile(dvc_lock_data):
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
        results.append({
            "path": out.get("path", ""),
            "md5": md5,
            "size_bytes": max(0, size_bytes),
            "is_dir": is_dir,
        })
    return results


def extract_node_deps_from_dvc_lock(
    dvc_lock_data: str | Mapping[str, Any],
    node_name: str,
) -> list[dict[str, Any]]:
    """
    Utility for W2/W3 to extract dependency definitions directly from dvc.lock
    for a given stage/node.
    
    Returns list of dicts:
      [{"path": "...", "md5": "...", "size_bytes": 1234, "is_dir": bool}]
    """
    if isinstance(dvc_lock_data, str):
        try:
            import yaml  # type: ignore
        except ImportError:
            import json as yaml  # type: ignore

        if "\n" not in dvc_lock_data and (dvc_lock_data.endswith(".lock") or dvc_lock_data.endswith(".yaml")):
            import os
            if os.path.isfile(dvc_lock_data):
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
    deps = stage.get("deps", [])

    results = []
    for dep in deps:
        if not isinstance(dep, Mapping):
            continue
        raw_hash = dep.get("md5") or dep.get("hash")
        if not raw_hash:
            continue
        md5 = normalize_hash(raw_hash)
        is_dir = bool(dep.get("is_dir") or md5.endswith(".dir"))
        size_bytes = int(dep.get("size_bytes", dep.get("size", 0)) or 0)
        results.append({
            "path": dep.get("path", ""),
            "md5": md5,
            "size_bytes": max(0, size_bytes),
            "is_dir": is_dir,
        })
    return results
